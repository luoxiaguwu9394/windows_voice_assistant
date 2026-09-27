"""
Render `config/config.template.yaml` into a user's `config/config.yaml`.

Deliberately a plain-text substitution, not a YAML load/dump round-trip: the
template's comments ARE the user documentation, and `yaml.safe_dump` would
erase them (it would also materialise the blanked `${VAR}` expansions — the
reason `ConfigManager.set()` is never called in the app).

Two token positions are supported:

* inline — `city: "@WEATHER_CITY@"`: the value replaces the token inside the
  line, quotes and all staying in the template;
* block — a line that is exactly one token: the whole line is replaced by the
  value, which may itself be several lines (the wake-word list). Indentation
  belongs to the value, not the template line.

A token without a provided value, or a token left over after rendering (a typo
in either the template or the values), raises — a half-rendered config must
never reach disk.
"""

from __future__ import annotations

import re
from typing import Dict, List

TOKEN_RE = re.compile(r"@([A-Z][A-Z0-9_]*)@")


class TemplateError(ValueError):
    """A token had no value, or the rendered output still contains tokens."""


def render_template(template_text: str, values: Dict[str, str]) -> str:
    missing = sorted(set(TOKEN_RE.findall(template_text)) - set(values))
    if missing:
        raise TemplateError(f"模板缺少这些占位符的取值: {', '.join(missing)}")

    out_lines: List[str] = []
    for line in template_text.splitlines():
        stripped = line.strip()
        match = TOKEN_RE.fullmatch(stripped)
        if match:
            # Block position: the whole line is one token.
            out_lines.append(values[match.group(1)].rstrip("\n"))
            continue
        out_lines.append(TOKEN_RE.sub(lambda m: values[m.group(1)], line))

    rendered = "\n".join(out_lines)
    if template_text.endswith("\n"):
        rendered += "\n"

    leftover = sorted(set(TOKEN_RE.findall(rendered)))
    if leftover:
        raise TemplateError(f"渲染后仍残留占位符（模板或取值有拼写错误）: {', '.join(leftover)}")
    return rendered


def yaml_keyword_block(keywords: List[str], indent: str = "  ") -> str:
    """
    The value for the `@KWS_KEYWORDS@` block: one YAML list item per line.

    Double quotes are escaped the way YAML expects, so a wake word containing
    a quote character cannot corrupt the generated file.
    """
    if not keywords:
        raise TemplateError("至少需要一个唤醒词")
    lines = [f'{indent}- "{_yaml_quote(word)}"' for word in keywords]
    return "\n".join(lines)


def _yaml_quote(word: str) -> str:
    return word.replace("\\", "\\\\").replace('"', '\\"')
