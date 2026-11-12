"""MCP server (stdio or streamable HTTP) + plain REST routes for non-MCP callers (Java backend, Postman, CI).

  stdio (Devin/Claude Desktop/Cursor spawn it):   python -m apichain.server
  HTTP  (shared, remote, Java):                   python -m apichain.server --transport http --port 8765
      MCP endpoint  -> http://host:8765/mcp
      REST endpoints-> GET  /api/workflows
                       POST /api/workflows/{name}/run      body: {"inputs": {...}}
                       POST /api/requests/{name}/call      body: {"variables": {...}}
"""
from __future__ import annotations

import argparse
import os
from typing import Any

from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

from .engine import Registry, _truncate

reg = Registry()
mcp = FastMCP("api-chain", host=os.environ.get("APICHAIN_HOST", "127.0.0.1"),
              port=int(os.environ.get("APICHAIN_PORT", "8765")))


# ---------------------------------------------------------------- MCP tools
@mcp.tool()
def list_workflows() -> list[dict]:
    """List multi-step API workflows (deterministic chains run server-side). Prefer run_workflow over chaining
    call_api yourself: it uses far fewer tokens and IDs are passed between steps by code, not by you."""
    return [{"name": n, "description": w.get("description", ""), "inputs": w.get("inputs", {}),
             "steps": [s.get("id", s["request"]) for s in w["steps"]], "outputs": list((w.get("output") or {}))}
            for n, w in reg.workflows.items()]


@mcp.tool()
def run_workflow(name: str, inputs: dict[str, Any] | None = None, include_response_bodies: bool = False) -> dict:
    """Run a named workflow end-to-end (e.g. order_flow: individual -> customer -> account -> order).
    Returns compact per-step status, extracted IDs and final outputs. Full request/response trace is saved
    to a file on the server; set include_response_bodies only when debugging."""
    return reg.run_workflow(name, inputs or {}, full_bodies=include_response_bodies)


@mcp.tool()
def list_apis(filter: str | None = None) -> list[dict]:
    """List individual API requests loaded from .http / curl / OpenAPI files, with the {{variables}} each needs."""
    items = [r.summary() for r in reg.requests.values()]
    if filter:
        f = filter.lower()
        items = [i for i in items if f in (i["name"] + i["url"] + i["description"]).lower()]
    return items


@mcp.tool()
def call_api(name: str, variables: dict[str, Any] | None = None) -> dict:
    """Call one API by name. Environment values (BASE_URL, tokens, defaults) are filled automatically;
    pass only request-specific variables. For OpenAPI operations pass the JSON payload as variables.body."""
    r = reg.call(name, variables or {})
    limit = reg.settings.get("max_body_chars_in_result", 1500)
    return {"status": r["status"], "ms": r["ms"], "url": r["url"], "body": _truncate(r["body"], limit)}


@mcp.tool()
def reload_definitions() -> dict:
    """Re-scan apis/, workflows/ and config/ after files are added or changed."""
    return reg.reload()


@mcp.resource("workflow://{name}")
def workflow_yaml(name: str) -> str:
    """Raw YAML of a workflow (useful when asking an LLM to write a new workflow from an existing one)."""
    import yaml
    return yaml.safe_dump(reg.workflows[name], sort_keys=False)


# ---------------------------------------------------------------- plain REST (HTTP transport only)
@mcp.custom_route("/api/workflows", methods=["GET"])
async def rest_list(_: Request) -> JSONResponse:
    return JSONResponse(list_workflows())


@mcp.custom_route("/api/workflows/{name}/run", methods=["POST"])
async def rest_run(request: Request) -> JSONResponse:
    payload = await request.json() if await request.body() else {}
    try:
        res = reg.run_workflow(request.path_params["name"], payload.get("inputs", {}),
                               full_bodies=payload.get("include_response_bodies", False))
        return JSONResponse(res, status_code=200 if res["ok"] else 422)
    except KeyError as e:
        return JSONResponse({"error": str(e)}, status_code=404)


@mcp.custom_route("/api/requests/{name}/call", methods=["POST"])
async def rest_call(request: Request) -> JSONResponse:
    payload = await request.json() if await request.body() else {}
    try:
        r = reg.call(request.path_params["name"], payload.get("variables", {}))
        r.pop("_sent", None)
        return JSONResponse(r)
    except KeyError as e:
        return JSONResponse({"error": str(e)}, status_code=404)


@mcp.custom_route("/health", methods=["GET"])
async def health(_: Request) -> JSONResponse:
    return JSONResponse({"ok": True, "env": reg.env_name, "requests": len(reg.requests),
                         "workflows": len(reg.workflows)})


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    ap.add_argument("--host")
    ap.add_argument("--port", type=int)
    a = ap.parse_args()
    if a.host:
        mcp.settings.host = a.host
    if a.port:
        mcp.settings.port = a.port
    mcp.run(transport="streamable-http" if a.transport == "http" else "stdio")


if __name__ == "__main__":
    main()
