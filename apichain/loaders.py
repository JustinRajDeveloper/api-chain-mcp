"""Load request definitions from .http files (REST-client syntax OR raw curl) and OpenAPI/Swagger specs."""
from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .template import variables_in

METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")


@dataclass
class RequestDef:
    name: str
    method: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    body: str | None = None
    source: str = ""
    description: str = ""
    json_body_param: bool = False  # OpenAPI ops: body passed as a 'body' var

    @property
    def variables(self) -> list[str]:
        return sorted(variables_in(self.url, self.body, *self.headers.values()))

    def summary(self) -> dict:
        return {"name": self.name, "method": self.method, "url": self.url,
                "variables": self.variables, "source": self.source, "description": self.description}


# ---------------------------------------------------------------- .http files
def _parse_curl(cmd: str) -> tuple[str, str, dict, str | None]:
    cmd = re.sub(r"\\\r?\n", " ", cmd)  # line continuations
    tokens = shlex.split(cmd)
    method, url, headers, body = None, None, {}, None
    i = 1
    while i < len(tokens):
        t = tokens[i]
        if t in ("-X", "--request"):
            method = tokens[i + 1].upper(); i += 2; continue
        if t in ("-H", "--header"):
            k, _, v = tokens[i + 1].partition(":"); headers[k.strip()] = v.strip(); i += 2; continue
        if t in ("-d", "--data", "--data-raw", "--data-binary", "--json"):
            body = tokens[i + 1]
            if t == "--json":
                headers.setdefault("Content-Type", "application/json")
            i += 2; continue
        if t in ("-u", "--user", "-o", "--output", "-w", "--write-out", "--connect-timeout", "-m", "--max-time"):
            i += 2; continue
        if t.startswith("-"):
            i += 1; continue  # -s, -k, -v, --location, --compressed ...
        url = t; i += 1
    if not url:
        raise ValueError("curl command without URL")
    return (method or ("POST" if body else "GET")), url, headers, body


def _parse_block(text: str, fallback_name: str, source: str) -> RequestDef | None:
    name, desc = fallback_name, ""
    lines: list[str] = []
    for line in text.splitlines():
        s = line.strip()
        m = re.match(r"^(?:#|//)\s*@name\s+(\S+)", s)
        if m:
            name = m.group(1); continue
        m = re.match(r"^(?:#|//)\s*@desc(?:ription)?\s+(.+)", s)
        if m:
            desc = m.group(1); continue
        if s.startswith("#") or s.startswith("//"):
            continue
        lines.append(line.rstrip())
    while lines and not lines[0].strip():
        lines.pop(0)
    if not lines:
        return None

    if lines[0].lstrip().startswith("curl "):
        method, url, headers, body = _parse_curl("\n".join(lines))
        return RequestDef(name, method, url, headers, body, source, desc)

    first = lines[0].split()
    if first[0].upper() in METHODS:
        method, url = first[0].upper(), first[1]
    else:  # "https://..." means GET
        method, url = "GET", first[0]
    headers: dict[str, str] = {}
    i = 1
    while i < len(lines) and lines[i].strip():
        k, _, v = lines[i].partition(":")
        headers[k.strip()] = v.strip(); i += 1
    body = "\n".join(lines[i + 1:]).strip() or None
    return RequestDef(name, method, url, headers, body, source, desc)


def load_http_file(path: Path) -> tuple[dict[str, RequestDef], dict[str, str]]:
    """Returns (requests, file_vars). File vars are lines like '@baseUrl = https://...'."""
    raw = path.read_text(encoding="utf-8")
    file_vars: dict[str, str] = {}
    kept = []
    for line in raw.splitlines():
        m = re.match(r"^@([\w.-]+)\s*=\s*(.*)$", line.strip())
        if m:
            file_vars[m.group(1)] = m.group(2).strip()
        else:
            kept.append(line)
    blocks = re.split(r"^###[^\S\n]*(.*)$", "\n".join(kept), flags=re.M)
    reqs: dict[str, RequestDef] = {}
    # blocks = [preamble, title1, body1, title2, body2, ...]
    pairs = [("", blocks[0])] + list(zip(blocks[1::2], blocks[2::2]))
    for n, (title, body) in enumerate(pairs):
        fallback = re.sub(r"\W+", "_", title.strip()).strip("_") or f"{path.stem}_{n}"
        rd = _parse_block(body, fallback, path.name)
        if rd:
            reqs[rd.name] = rd
    return reqs, file_vars


# ---------------------------------------------------------------- OpenAPI / Swagger
def load_openapi(path: Path, base_url_var: str = "BASE_URL") -> dict[str, RequestDef]:
    spec = yaml.safe_load(path.read_text(encoding="utf-8")) if path.suffix in (".yaml", ".yml") \
        else json.loads(path.read_text(encoding="utf-8"))
    if "openapi" not in spec and "swagger" not in spec:
        return {}
    reqs: dict[str, RequestDef] = {}
    for p, ops in (spec.get("paths") or {}).items():
        for method, op in ops.items():
            if method.upper() not in METHODS:
                continue
            name = op.get("operationId") or re.sub(r"\W+", "_", f"{method}_{p}").strip("_")
            url = "{{" + base_url_var + "}}" + re.sub(r"\{(\w+)\}", r"{{\1}}", p)
            q = [prm["name"] for prm in op.get("parameters", []) if prm.get("in") == "query" and prm.get("required")]
            if q:
                url += "?" + "&".join(f"{k}={{{{{k}}}}}" for k in q)
            has_body = "requestBody" in op or any(prm.get("in") == "body" for prm in op.get("parameters", []))
            reqs[name] = RequestDef(
                name=name, method=method.upper(), url=url,
                headers={"Content-Type": "application/json"} if has_body else {},
                body="{{body}}" if has_body else None, source=path.name,
                description=op.get("summary") or op.get("description", "")[:200],
                json_body_param=has_body,
            )
    return reqs
