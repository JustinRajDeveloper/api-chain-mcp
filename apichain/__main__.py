"""CLI — run chains with zero LLM involvement (CI, cron, Java ProcessBuilder, your terminal).

  python -m apichain list
  python -m apichain run order_flow --set firstName=John --set lastName=Doe [--env qa] [--bodies]
  python -m apichain call createIndividual --set firstName=John
  python -m apichain run order_flow --inputs-file data/john.json
Exit code 0 = success, 1 = a step failed. Output is JSON on stdout.
"""
from __future__ import annotations

import argparse
import json
import sys

from .engine import Registry


def _kv(pairs: list[str]) -> dict:
    out = {}
    for p in pairs or []:
        k, _, v = p.partition("=")
        try:
            out[k] = json.loads(v)  # allows numbers / objects: --set qty=2 --set body='{"a":1}'
        except ValueError:
            out[k] = v
    return out


def main() -> int:
    ap = argparse.ArgumentParser(prog="apichain")
    ap.add_argument("--env", help="environment name from config/settings.yaml (default APICHAIN_ENV or dev)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    r = sub.add_parser("run"); r.add_argument("workflow"); r.add_argument("--set", action="append")
    r.add_argument("--inputs-file"); r.add_argument("--bodies", action="store_true")
    c = sub.add_parser("call"); c.add_argument("request"); c.add_argument("--set", action="append")
    a = ap.parse_args()

    reg = Registry(env=a.env)
    if a.cmd == "list":
        print(json.dumps({"env": reg.env_name,
                          "workflows": {n: w.get("description", "") for n, w in reg.workflows.items()},
                          "requests": [x.summary() for x in reg.requests.values()]}, indent=2))
        return 0
    if a.cmd == "run":
        inputs = json.load(open(a.inputs_file)) if a.inputs_file else {}
        inputs.update(_kv(a.set))
        res = reg.run_workflow(a.workflow, inputs, full_bodies=a.bodies)
        print(json.dumps(res, indent=2, default=str))
        return 0 if res["ok"] else 1
    res = reg.call(a.request, _kv(a.set))
    res.pop("headers", None)
    print(json.dumps(res, indent=2, default=str))
    return 0 if res["status"] < 400 else 1


if __name__ == "__main__":
    sys.exit(main())
