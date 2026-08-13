"""Shared YAML sanity checks and error formatting.

Two hand-written YAML files decide whether a run works: git_locate's own
``config.yaml`` and Notify's provider config. Both fail in ways whose default
error message says nothing useful — a PyYAML traceback, or Notify's bare
``could not parse provider config file``. The helpers here turn those into
something an operator can act on: the offending line, a caret under the column,
and the keys that collide.
"""
from __future__ import annotations

from typing import List, NamedTuple, Optional

try:
    import yaml  # type: ignore
except Exception:  # pragma: no cover - dependency missing
    yaml = None


class DuplicateKey(NamedTuple):
    """A key defined more than once inside the same YAML mapping."""

    key: str
    path: str          # dotted path of the parent mapping ("" = document root)
    line: int          # 1-based line of the repeat
    first_line: int    # 1-based line of the first definition


def _walk(node, path: str, found: List[DuplicateKey]) -> None:
    if isinstance(node, yaml.MappingNode):
        seen = {}
        for key_node, value_node in node.value:
            key = str(getattr(key_node, "value", key_node))
            line = key_node.start_mark.line + 1
            if key in seen:
                found.append(DuplicateKey(key=key, path=path, line=line,
                                          first_line=seen[key]))
            else:
                seen[key] = line
            _walk(value_node, f"{path}.{key}" if path else key, found)
    elif isinstance(node, yaml.SequenceNode):
        for index, child in enumerate(node.value):
            _walk(child, f"{path}[{index}]", found)


def find_duplicate_keys(text: str) -> List[DuplicateKey]:
    """Find keys defined twice in the same mapping, anywhere in ``text``.

    PyYAML silently keeps the last value; Notify's Go parser rejects the file
    outright. Composing the node tree (rather than loading it) keeps the source
    line of every key so the report can point at what to merge.

    Raises ``yaml.YAMLError`` if the text does not parse at all.
    """
    found: List[DuplicateKey] = []
    for document in yaml.compose_all(text):
        if document is not None:
            _walk(document, "", found)
    return found


def _source_line(path: str, index: int) -> Optional[str]:
    """Return line ``index`` (0-based) of ``path``, or None if unreadable."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for i, line in enumerate(fh):
                if i == index:
                    return line.rstrip("\n")
    except OSError:
        return None
    return None


def format_yaml_error(path: str, exc: Exception) -> str:
    """Render a PyYAML error as a quoted source line with a caret and a hint."""
    problem = getattr(exc, "problem", None)
    mark = getattr(exc, "problem_mark", None)
    if mark is None:
        return f"{path} is not valid YAML: {str(exc).strip()}"

    lines = [f"{path} is not valid YAML: {problem or 'parse error'} "
             f"(line {mark.line + 1}, column {mark.column + 1})"]
    source = _source_line(path, mark.line)
    if source is not None:
        gutter = f"  {mark.line + 1} | "
        lines.append(f"{gutter}{source}")
        lines.append(" " * (len(gutter) + mark.column) + "^")
    context = getattr(exc, "context", None)
    context_mark = getattr(exc, "context_mark", None)
    if context and context_mark is not None:
        lines.append(f"  ({context}, started at line {context_mark.line + 1})")
    if "sequence" in (problem or ""):
        lines.append("  Hint: list items must be indented further than the key "
                     "they belong to. When uncommenting an example, remove only "
                     "the '# ' and keep the leading spaces:")
        lines.append("    commands:")
        lines.append('      - "trufflehog git {repo_url} --json"')
    return "\n".join(lines)
