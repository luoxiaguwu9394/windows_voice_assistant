"""
Comment-preserving, atomic edits to the rendered `config/config.yaml`.

The settings UI (winvoice.webui) is the first runtime writer of the config
file, and it has two hard constraints:

* **Atomic.** The assistant's watchdog reload (config.py, 0.5 s debounce)
  reads the file on every change; a truncate-then-write window can hand it a
  half-written YAML, whose parse failure kills the observer thread *silently*
  — every later hot reload would be dead until restart. So: write a temp file
  in the same directory, then `os.replace` (atomic on Windows and POSIX).
* **Comment-preserving.** The config is the user's reference documentation
  (400 lines of hard-won explanations). `yaml.safe_dump` round-trips the data
  but discards every comment, so the default path is line surgery: locate the
  key's line (scalars) or its indented block (lists), rewrite just that, keep
  everything else byte-identical. Surgery only handles the two-level
  `section.key` paths this UI manages; anything else — or a structure the
  locator does not recognise — makes `update_keys` return None and the caller
  falls back to a full `safe_dump` (data survives, comments do not; the UI
  says so).

All-or-nothing: if any requested path cannot be located, *no* partial text is
produced — either every change lands via surgery or the caller falls back for
all of them.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Any, Optional

import yaml


def _dump_fragment(value: Any) -> str:
    """
    YAML for a value as it sits on its own line(s), without a document end.

    `yaml.safe_dump` of a scalar root emits `...` (document end) and may wrap
    long strings; the fragment must be bare. A genuine string value that ends
    in "..." is quoted by the dumper, so popping a bare trailing `...` line
    can never eat user data.
    """
    dumped = yaml.safe_dump(
        value, allow_unicode=True, default_flow_style=False, width=1_000_000,
        sort_keys=False,
    )
    lines = dumped.rstrip("\n").split("\n")
    if lines[-1] == "...":
        lines.pop()
    return "\n".join(lines)


def update_keys(text: str, changes: dict[str, Any]) -> Optional[str]:
    """
    Apply `{dotted.path: new_value}` edits to `text` and return the new text.

    Supports exactly two-level paths (`tts.speed`, `kws.keywords`,
    `tools.apps`) — the shape of every key the settings UI manages. Returns
    None when any path cannot be located at the expected level; the caller
    then falls back to a full dump rather than writing a half-understood file.
    """
    if not changes:
        return text
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.split(newline)
    # Positions are computed against the original list; every edit below only
    # rewrites lines it owns (a key line, or a list block it has located), so
    # indices stay valid without recomputation.
    spans: list[tuple[int, Optional[int], list[str]]] = []  # (start, end, replacement)

    for dotted, value in changes.items():
        parts = dotted.split(".")
        if len(parts) != 2:
            return None
        located = _locate_key(lines, parts[0], parts[1])
        if located is None:
            return None
        key_index, key_indent = located
        if isinstance(value, list):
            spans.append(_plan_list(lines, key_index, key_indent, parts[1], value))
        else:
            spans.append((_plan_scalar(lines, key_index, dotted, value)))

    # Apply from the bottom up so earlier indices never shift.
    for start, end, replacement in sorted(spans, key=lambda span: -span[0]):
        lines[start:end] = replacement
    return newline.join(lines)


def _locate_key(lines: list[str], section: str, key: str) -> Optional[tuple[int, int]]:
    """The line index and indent of `section.key`, or None."""
    section_re = re.compile(rf"^{re.escape(section)}\s*:")
    key_re = re.compile(rf"^(\s+){re.escape(key)}\s*:")
    in_section = False
    for index, line in enumerate(lines):
        if section_re.match(line) and not line.startswith((" ", "\t")):
            in_section = True
            continue
        if not in_section:
            continue
        if line and not line.startswith((" ", "\t")):
            break  # the next top-level section: the key is not in this one
        match = key_re.match(line)
        if match:
            return index, len(match.group(1))
    return None


def _plan_scalar(lines: list[str], key_index: int, dotted: str, value: Any) -> tuple[int, None, list[str]]:
    """Rewrite the key line's value, keeping any trailing `# comment`."""
    line = lines[key_index]
    key = dotted.split(".")[1]
    before, match, after = line.partition(f"{key}:")
    if not match:  # _locate_key matched this same pattern; belt and braces
        return key_index, None, [line]
    # `after` holds the old value and possibly an inline comment. A `#` only
    # starts a comment outside quotes; these keys' values are plain strings,
    # numbers or booleans, so the first `#` outside quotes is the comment.
    value_part, comment = _split_inline_comment(after)
    leading = value_part[: len(value_part) - len(value_part.lstrip())]
    rendered = _dump_fragment(value)
    rebuilt = f"{before}{match}{leading}{rendered}"
    if comment:
        rebuilt = f"{rebuilt}  {comment}"
    # Exclusive end = the next line: the scalar owns exactly its own line.
    return key_index, key_index + 1, [rebuilt]


def _split_inline_comment(rest: str) -> tuple[str, str]:
    """Split ` 0.9   # note` (or ` 北京`) into (value-with-spacing, comment)."""
    in_quote = False
    quote = ""
    for index, char in enumerate(rest):
        if in_quote:
            if char == quote:
                in_quote = False
            continue
        if char in "\"'":
            in_quote = True
            quote = char
        elif char == "#":
            return rest[:index].rstrip(), "#" + rest[index + 1 :].rstrip()
    return rest.rstrip(), ""


def _plan_list(lines: list[str], key_index: int, key_indent: int, key: str, value: list) -> tuple[int, Optional[int], list[str]]:
    """Replace (or install) the indented block under a list-valued key."""
    prefix = " " * key_indent
    if not value:
        return key_index, _block_end(lines, key_index, key_indent), [f"{prefix}{key}: []"]

    item_indent = key_indent + 2
    rendered: list[str] = [f"{prefix}{key}:"]  # the key line itself survives
    for item in value:
        if isinstance(item, dict):
            rows = _dump_fragment(item).split("\n")
            rendered.append(f"{' ' * item_indent}- {rows[0]}")
            rendered.extend(f"{' ' * (item_indent + 2)}{row}" for row in rows[1:])
        else:
            text = _dump_fragment(item)
            quoted = f'"{text}"' if not text.startswith(('"', "'")) else text
            rendered.append(f"{' ' * item_indent}- {quoted}")
    return key_index, _block_end(lines, key_index, key_indent), rendered


def _block_end(lines: list[str], key_index: int, key_indent: int) -> Optional[int]:
    """Exclusive end of the indented block under the key line (None = EOF)."""
    end = None
    for index in range(key_index + 1, len(lines)):
        line = lines[index]
        if not line.strip():  # blank lines belong to the block only if followed by more of it
            end = index + 1
            continue
        if line.startswith(" " * (key_indent + 1)) or line.startswith("\t"):
            end = index + 1
        else:
            end = index
            break
    return end


def save_config(path: Path, changes: dict[str, Any]) -> dict:
    """
    Apply `changes` to the config file at `path`, atomically.

    Returns `{"mode": "surgical" | "full", "keys": [...]}` — `surgical` kept
    every comment, `full` means the structure was not recognised and the file
    was rewritten from parsed data (values survive, comments do not; the UI
    should say so). Raises only if the file cannot be read or written.
    """
    text = path.read_text(encoding="utf-8")
    new_text = update_keys(text, changes)
    mode = "surgical"
    if new_text is None:
        data = yaml.safe_load(text) or {}
        for dotted, value in changes.items():
            parts = dotted.split(".")
            section = data.get(parts[0])
            if len(parts) == 2:
                # A section that is not a mapping (hand-edited into nonsense)
                # is replaced wholesale — the fallback's job is to land the
                # requested value as valid YAML, not to preserve the wreck.
                if not isinstance(section, dict):
                    section = {}
                    data[parts[0]] = section
                section[parts[1]] = value
            else:
                data[dotted] = value
        new_text = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
        mode = "full"

    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=path.name + ".", suffix=".tmp", delete=False,
    )
    try:
        with handle:
            handle.write(new_text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(handle.name, path)
    except BaseException:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise
    return {"mode": mode, "keys": sorted(changes)}
