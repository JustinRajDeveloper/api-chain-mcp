"""Registry (loads config, .http, swagger, workflows) + deterministic executor for single calls and chains."""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import yaml
from dotenv import load_dotenv

from .loaders import RequestDef, load_http_file, load_openapi
from .template import MissingVariables, extract, render

SECRET_HEADERS = {"authorization", "x-api-key", "api-key", "cookie", "ocp-apim-subscription-key"}
ENV_REF = re.compile(r"\$\{(\w+)(?::-([^}]*))?\}")


def _expand_env(v: Any) -> Any:
    if isinstance(v, str):
        return ENV_REF.sub(lambda m: os.environ.get(m.group(1), m.group(2) or ""), v)
    if isinstance(v, dict):
        return {k: _expand_env(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_expand_env(x) for x in v]
    return v


def _truncate(obj: Any, limit: int) -> Any:
    s = obj if isinstance(obj, str) else json.dumps(obj, default=str)
    return obj if len(s) <= limit else s[:limit] + f"...<truncated {len(s) - limit} chars>"


@dataclass
class StepResult:
    id: str
    request: str
    status: int | None
    ms: int
    ok: bool
    extracted: dict = field(default_factory=dict)
    error: str | None = None
    attempts: int = 1


class Registry:
    def __init__(self, root: str | Path | None = None, env: str | None = None):
        self.root = Path(root or os.environ.get("APICHAIN_HOME", Path(__file__).resolve().parent.parent))
        self.env_name = env or os.environ.get("APICHAIN_ENV", "dev")
        self.reload()

    # ------------------------------------------------------------ loading
    def reload(self) -> dict:
        load_dotenv(self.root / ".env", override=False)
        load_dotenv(self.root / f".env.{self.env_name}", override=False)
        cfg_path = self.root / "config" / "settings.yaml"
        cfg = yaml.safe_load(cfg_path.read_text()) if cfg_path.exists() else {}
        self.settings = cfg.get("settings", {})
        envs = cfg.get("environments", {})
        if envs and self.env_name not in envs:
            raise ValueError(f"Environment '{self.env_name}' not in settings.yaml ({list(envs)})")
        self.vars: dict[str, Any] = _expand_env(envs.get(self.env_name, {}))
        self.default_headers: dict[str, str] = _expand_env(cfg.get("default_headers", {}))

        self.requests: dict[str, RequestDef] = {}
        self.file_vars: dict[str, str] = {}
        for p in sorted((self.root / "apis").rglob("*")):
            if p.suffix == ".http" or p.suffix == ".rest":
                reqs, fvars = load_http_file(p)
                self.requests.update(reqs)
                self.file_vars.update(fvars)
            elif p.suffix in (".json", ".yaml", ".yml"):
                self.requests.update(load_openapi(p, self.settings.get("openapi_base_url_var", "BASE_URL")))

        self.workflows: dict[str, dict] = {}
        for p in sorted((self.root / "workflows").glob("*.y*ml")):
            wf = yaml.safe_load(p.read_text())
            self.workflows[wf.get("name", p.stem)] = wf
        return {"env": self.env_name, "requests": len(self.requests), "workflows": len(self.workflows)}

    def base_context(self) -> dict:
        # precedence: .http @vars < settings.yaml env vars  (inputs/extracted layered on top later)
        ctx: dict[str, Any] = {}
        for k, v in self.file_vars.items():
            try:
                ctx[k] = render(v, {**self.file_vars, **self.vars})
            except MissingVariables:
                ctx[k] = v
        ctx.update(self.vars)
        return ctx

    # ------------------------------------------------------------ single call
    def call(self, name: str, variables: dict | None = None, ctx: dict | None = None,
             client: httpx.Client | None = None) -> dict:
        if name not in self.requests:
            raise KeyError(f"Unknown request '{name}'. Known: {sorted(self.requests)}")
        rd = self.requests[name]
        c = {**(ctx if ctx is not None else self.base_context()), **(variables or {})}
        where = f"in request '{name}'"
        url = render(rd.url, c, where=where)
        headers = {**self.default_headers, **rd.headers}
        headers = {k: render(v, c, where=where) for k, v in headers.items()}
        is_json = "json" in headers.get("Content-Type", headers.get("content-type", "")).lower()
        body = render(rd.body, c, json_escape=is_json, where=where)

        own = client is None
        client = client or httpx.Client(timeout=self.settings.get("timeout_seconds", 30),
                                        verify=self.settings.get("verify_tls", True))
        try:
            t0 = time.perf_counter()
            resp = client.request(rd.method, url, headers=headers, content=body.encode() if body else None)
            ms = int((time.perf_counter() - t0) * 1000)
        finally:
            if own:
                client.close()
        try:
            data = resp.json()
        except ValueError:
            data = resp.text
        return {"request": name, "method": rd.method, "url": url, "status": resp.status_code, "ms": ms,
                "headers": dict(resp.headers), "body": data,
                "_sent": {"headers": {k: ("***" if k.lower() in SECRET_HEADERS else v) for k, v in headers.items()},
                          "body": body}}

    # ------------------------------------------------------------ workflow
    def run_workflow(self, name: str, inputs: dict | None = None, *, save_trace: bool = True,
                     full_bodies: bool = False) -> dict:
        if name not in self.workflows:
            raise KeyError(f"Unknown workflow '{name}'. Known: {sorted(self.workflows)}")
        wf = self.workflows[name]
        inputs = dict(inputs or {})
        spec_inputs = wf.get("inputs", {}) or {}
        missing = [k for k, s in spec_inputs.items()
                   if k not in inputs and (s or {}).get("default") is None and (s or {}).get("required", True)]
        if missing:
            return {"workflow": name, "ok": False, "error": f"Missing required inputs: {missing}",
                    "inputs_spec": spec_inputs}

        ctx = self.base_context()
        for k, s in spec_inputs.items():
            if k not in inputs and (s or {}).get("default") is not None:
                inputs[k] = render(str(s["default"]), ctx)  # defaults may use {{$uuid}} etc.
        ctx.update(inputs)

        steps: list[StepResult] = []
        trace: list[dict] = []
        ok = True
        started = datetime.now()
        with httpx.Client(timeout=self.settings.get("timeout_seconds", 30),
                          verify=self.settings.get("verify_tls", True)) as client:
            for step in wf["steps"]:
                # skip_if: individualId  -> skip this step when the caller already supplied that value
                skip_vars = step.get("skip_if") or []
                skip_vars = [skip_vars] if isinstance(skip_vars, str) else skip_vars
                if skip_vars and all(ctx.get(v) not in (None, "") for v in skip_vars):
                    steps.append(StepResult(step.get("id", step["request"]), step["request"], None, 0, True,
                                            {v: ctx[v] for v in skip_vars}, error=None, attempts=0))
                    continue
                if "when" in step:
                    try:
                        cond = str(render(step["when"], ctx)).lower()
                    except MissingVariables:
                        cond = "false"
                    if cond in ("false", "0", "", "none"):
                        continue
                sr, raw = self._run_step(step, ctx, client)
                steps.append(sr)
                trace.append(raw)
                if not sr.ok:
                    ok = False
                    if not step.get("continue_on_error", False):
                        break

        outputs = {}
        if ok:
            for k, tmpl in (wf.get("output") or {}).items():
                try:
                    outputs[k] = render(str(tmpl), ctx)
                except MissingVariables as e:
                    outputs[k] = f"<missing {e.names}>"

        limit = self.settings.get("max_body_chars_in_result", 1500)
        result = {
            "workflow": name, "env": self.env_name, "ok": ok,
            "outputs": outputs,
            "steps": [{"id": s.id, "status": s.status, "ms": s.ms, "ok": s.ok, "attempts": s.attempts,
                       **({"extracted": s.extracted} if s.extracted else {}),
                       **({"error": _truncate(s.error, limit)} if s.error else {})} for s in steps],
        }
        if full_bodies:
            result["responses"] = {t["step"]: t.get("response", {}).get("body") for t in trace}
        if save_trace and self.settings.get("save_traces", True):
            runs = self.root / "runs"
            runs.mkdir(exist_ok=True)
            f = runs / f"{started:%Y%m%d-%H%M%S}-{name}.json"
            f.write_text(json.dumps({"result": result, "inputs": inputs, "trace": trace}, indent=2, default=str))
            result["trace_file"] = str(f)
        return result

    def _run_step(self, step: dict, ctx: dict, client: httpx.Client) -> tuple[StepResult, dict]:
        sid = step.get("id", step["request"])
        expect = step.get("expect_status", [200, 201, 202])
        expect = [expect] if isinstance(expect, int) else expect
        retry = step.get("retry", {}) or {}
        attempts = int(retry.get("attempts", 1))
        delay = retry.get("delay_ms", 1000) / 1000
        step_vars = {k: render(str(v), ctx) for k, v in (step.get("with") or {}).items()}
        raw: dict = {"step": sid}
        resp: dict | None = None
        err = None
        for attempt in range(1, attempts + 1):
            try:
                resp = self.call(step["request"], step_vars, ctx=ctx, client=client)
                raw = {"step": sid, "attempt": attempt, "url": resp["url"], "sent": resp["_sent"],
                       "response": {"status": resp["status"], "body": resp["body"]}}
                if resp["status"] in expect:
                    err = None
                    break
                err = f"HTTP {resp['status']} (expected {expect}): {_truncate(resp['body'], 800)}"
            except MissingVariables as e:
                return StepResult(sid, step["request"], None, 0, False, error=str(e)), {"step": sid, "error": str(e)}
            except httpx.HTTPError as e:
                err = f"{type(e).__name__}: {e}"
                raw = {"step": sid, "attempt": attempt, "error": err}
            if attempt < attempts:
                time.sleep(delay)
        if err:
            return StepResult(sid, step["request"], resp and resp["status"], resp["ms"] if resp else 0, False,
                              error=err, attempts=attempt), raw

        extracted = {}
        source = {"body": resp["body"], "headers": resp["headers"], "status": resp["status"]}
        for var, path in (step.get("extract") or {}).items():
            try:
                if path.startswith("header:"):
                    hk = path[7:].strip().lower()
                    val = next(v for k, v in resp["headers"].items() if k.lower() == hk)
                else:
                    val = extract(resp["body"], path)
            except (KeyError, IndexError, TypeError, StopIteration):
                return StepResult(sid, step["request"], resp["status"], resp["ms"], False,
                                  error=f"extract '{var}' failed: path {path} not found in "
                                        f"{_truncate(resp['body'], 600)}", attempts=attempt), raw
            extracted[var] = val
        ctx.update(extracted)
        ctx[sid] = source  # allows {{individual.body.someField}} later
        return StepResult(sid, step["request"], resp["status"], resp["ms"], True, extracted, attempts=attempt), raw
