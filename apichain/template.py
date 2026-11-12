"""{{var}} templating + JSON-path extraction. No external deps."""
from __future__ import annotations

import json
import random
import re
import time
import uuid
from typing import Any

VAR_RE = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")


class MissingVariables(Exception):
    def __init__(self, names: set[str], where: str = ""):
        self.names = sorted(names)
        super().__init__(f"Missing variables {self.names} {where}".strip())


def _builtin(name: str) -> Any:
    """REST-client style dynamic values: {{$uuid}}, {{$timestamp}}, {{$randomInt}}."""
    if name == "$uuid":
        return str(uuid.uuid4())
    if name == "$timestamp":
        return str(int(time.time()))
    if name == "$timestampMs":
        return str(int(time.time() * 1000))
    if name == "$randomInt":
        return str(random.randint(100000, 999999))
    raise KeyError(name)


def lookup(ctx: dict, dotted: str) -> Any:
    """Resolve 'a', 'a.b', 'a.b[0].c' against a nested dict."""
    if dotted.startswith("$") and not dotted.startswith("$."):
        return _builtin(dotted)
    if dotted in ctx:  # flat key wins (keys may contain dots)
        return ctx[dotted]
    return extract({"_": ctx}, "$._." + dotted)


def render(text: str | None, ctx: dict, json_escape: bool = False, where: str = "") -> str | None:
    """Replace {{var}}. When json_escape, string values are escaped so they are safe inside JSON quotes."""
    if text is None:
        return None
    missing: set[str] = set()

    def sub(m: re.Match) -> str:
        key = m.group(1)
        try:
            val = lookup(ctx, key)
        except (KeyError, IndexError, TypeError):
            missing.add(key)
            return m.group(0)
        if val is None:
            missing.add(key)
            return m.group(0)
        if isinstance(val, (dict, list)):
            return json.dumps(val)
        if isinstance(val, bool):
            return "true" if val else "false"
        s = str(val)
        return json.dumps(s)[1:-1] if json_escape else s

    out = VAR_RE.sub(sub, text)
    if missing:
        raise MissingVariables(missing, where)
    return out


def variables_in(*texts: str | None) -> set[str]:
    found: set[str] = set()
    for t in texts:
        if t:
            found.update(v.strip() for v in VAR_RE.findall(t) if not v.strip().startswith("$"))
    return found


_TOKEN_RE = re.compile(r"\.([^.\[\]]+)|\[(\d+|\*)\]|\[['\"]([^'\"]+)['\"]\]")


def extract(data: Any, path: str) -> Any:
    """Tiny JSONPath subset: $.a.b, $.items[0].id, $['odd-key'], $.items[*].id"""
    if path in ("$", ""):
        return data
    if not path.startswith("$"):
        path = "$." + path
    cur: list[Any] = [data]
    wildcard = False
    for m in _TOKEN_RE.finditer(path[1:]):
        key, idx, qkey = m.groups()
        nxt: list[Any] = []
        for node in cur:
            if idx == "*":
                wildcard = True
                nxt.extend(node if isinstance(node, list) else [])
            elif idx is not None:
                nxt.append(node[int(idx)])
            else:
                nxt.append(node[key or qkey])
        cur = nxt
    return cur if wildcard else cur[0]
