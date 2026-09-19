"""
Constrain-decoding grammar and response normalisation for intent classification.

The small local model only ever emits an intent label plus structured args;
tool selection stays in code. To make that guarantee real, the generation is
constrained by a GBNF grammar so the output is always valid JSON.

Grammar notes (llama.cpp GBNF dialect):
- The entry rule MUST be named `root`. The `?start:` / `rule:` form used by
  some other grammars is not accepted and makes llama-server answer
  `400 Failed to parse grammar`.
- Character classes keep escapes minimal (`[^"\\\\]`), because complex escapes
  such as `\\x7F` are not portable across builds.

Even with a grammar, the model may pick its own argument key names
(`app_name` instead of `app`), so `normalize_args()` maps known synonyms onto
the keys the tool layer expects, and coerces a bare-string `args` value.
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

from winvoice.contracts import IntentName

# Intents the classifier may emit (UNKNOWN is the failure mode).
CLASSIFIABLE_INTENTS: List[IntentName] = [i for i in IntentName if i != IntentName.UNKNOWN]

_INTENT_ALT = " | ".join(f'"{i.value}"' for i in CLASSIFIABLE_INTENTS + [IntentName.UNKNOWN])

# Validated against llama.cpp b7376 (see scripts/check_llm.py).
INTENT_GRAMMAR: str = (
    'root ::= "{" ws "\\"intent\\"" ws ":" ws intent ws "," ws "\\"args\\"" ws ":" ws obj ws "}"\n'
    f"intent ::= \"\\\"\" ({_INTENT_ALT}) \"\\\"\"\n"
    'obj ::= "{" ws (pair (ws "," ws pair)*)? ws "}"\n'
    'pair ::= str ws ":" ws val\n'
    'val ::= str | num | "true" | "false" | "null" | obj | arr\n'
    'arr ::= "[" ws (val (ws "," ws val)*)? ws "]"\n'
    'str ::= "\\"" char* "\\""\n'
    'char ::= [^"\\\\] | "\\\\" ["\\\\/bfnrt]\n'
    'num ::= "-"? [0-9]+ ("." [0-9]+)?\n'
    "ws ::= [ \\t\\n]*\n"
)

# Argument keys each intent expects, plus a short description for the prompt.
INTENT_ARG_SPEC: Dict[IntentName, str] = {
    IntentName.OPEN_APP: '{"app": "<application name>"}',
    IntentName.CLOSE_APP: '{"app": "<application name>"}',
    IntentName.SET_VOLUME: '{"delta": <integer, negative to lower>}',
    IntentName.MEDIA_CONTROL: '{"action": "play|pause|next|prev"}',
    IntentName.SEARCH_WEB: '{"query": "<search terms>"}',
    IntentName.READ_FILE: '{"path": "<file path>"}',
    IntentName.WRITE_FILE: '{"path": "<file path>", "content": "<text>"}',
    IntentName.RUN_SCRIPT: '{"path": "<script path>"}',
    IntentName.GET_TIME: "{}",
    IntentName.GET_WEATHER: "{}",
}

# Synonym -> canonical key. Models (especially small ones) invent names.
ARG_ALIASES: Dict[str, str] = {
    # app
    "app_name": "app", "appname": "app", "application": "app",
    "application_name": "app", "name": "app", "target": "app", "program": "app",
    # volume
    "value": "delta", "amount": "delta", "level": "delta",
    "change": "delta", "volume": "delta", "volume_delta": "delta",
    # media
    "command": "action", "media_action": "action", "operation": "action",
    # search
    "query_text": "query", "keyword": "query", "keywords": "query",
    "search_query": "query", "text": "query", "terms": "query",
    # paths
    "file": "path", "filename": "path", "file_path": "path", "filepath": "path",
    "script": "path", "script_path": "path", "scriptpath": "path",
    # write content
    "body": "content", "data": "content", "text_content": "content",
}

# Which canonical key a bare string `args` should be coerced into.
_STRING_ARG_KEY: Dict[IntentName, str] = {
    IntentName.OPEN_APP: "app",
    IntentName.CLOSE_APP: "app",
    IntentName.SEARCH_WEB: "query",
    IntentName.READ_FILE: "path",
    IntentName.WRITE_FILE: "path",
    IntentName.RUN_SCRIPT: "path",
}


def build_intent_prompt(text: str) -> str:
    """Prompt that names each intent's argument schema, reducing key drift."""
    lines = [
        "You are an intent classifier for a Windows voice assistant.",
        "Choose exactly one intent and fill its arguments.",
        "",
        "Intents and their argument objects:",
    ]
    for intent, spec in INTENT_ARG_SPEC.items():
        lines.append(f'  {intent.value}: {spec}')
    lines += [
        f'  {IntentName.UNKNOWN.value}: {{}}   (use when nothing fits)',
        "",
        f'User said: "{text}"',
        "",
        "Reply with JSON only, matching the grammar. No prose, no markdown.",
    ]
    return "\n".join(lines)


def normalize_args(intent: IntentName, raw_args: Any) -> Dict[str, Any]:
    """
    Coerce model output into the argument dict the tool layer expects.

    Handles three failure modes seen in practice:
      - `args` is a bare string instead of an object
      - keys are synonyms (`app_name` instead of `app`)
      - set_volume delivers a stringified number
    """
    if raw_args is None or raw_args == "":
        return {}

    # A bare string means the model collapsed the object.
    if isinstance(raw_args, str):
        key = _STRING_ARG_KEY.get(intent)
        if key is None:
            return {}
        value = raw_args.strip()
        return {key: value} if value else {}

    if not isinstance(raw_args, dict):
        return {}

    out: Dict[str, Any] = {}
    for key, value in raw_args.items():
        canonical = ARG_ALIASES.get(str(key).lower().strip(), str(key).strip())
        out[canonical] = value

    if intent == IntentName.SET_VOLUME and "delta" in out:
        out["delta"] = _coerce_int(out["delta"])

    if intent == IntentName.MEDIA_CONTROL and "action" in out:
        action = str(out["action"]).lower().strip()
        out["action"] = {"resume": "play", "stop": "pause", "previous": "prev"}.get(action, action)

    return out


def _coerce_int(value: Any) -> Any:
    """Best-effort conversion to int, keeping the original on failure."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return value


def parse_intent_json(content: str) -> Tuple[IntentName, Dict[str, Any], float]:
    """
    Parse a (grammar-constrained) model response.

    Returns (intent, args, confidence). A missing/unrecognised intent yields
    IntentName.UNKNOWN with confidence 0.0.
    """
    import json

    try:
        parsed = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return IntentName.UNKNOWN, {}, 0.0

    if not isinstance(parsed, dict):
        return IntentName.UNKNOWN, {}, 0.0

    raw_intent = str(parsed.get("intent", "")).strip()
    try:
        intent = IntentName(raw_intent)
    except ValueError:
        return IntentName.UNKNOWN, {}, 0.0

    args = normalize_args(intent, parsed.get("args"))
    # Grammar-constrained decode is either well formed or it is not; there is
    # no calibrated probability, so report a fixed score for valid parses.
    confidence = 0.0 if intent == IntentName.UNKNOWN else 0.8
    return intent, args, confidence


__all__ = [
    "CLASSIFIABLE_INTENTS",
    "INTENT_GRAMMAR",
    "INTENT_ARG_SPEC",
    "ARG_ALIASES",
    "build_intent_prompt",
    "normalize_args",
    "parse_intent_json",
]
