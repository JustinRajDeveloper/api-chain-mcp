# api-chain-mcp

An MCP server plus a code runner that turns `.http` / curl / Swagger files into **atomic API tools** and turns
YAML workflows into **chains that run in code**. One engine serves four kinds of callers:

| Caller | How it connects |
|---|---|
| Devin, Claude Desktop/Code, Cursor, Copilot agent | MCP over **stdio** (`python -m apichain.server`) or **HTTP** (`http://host:8765/mcp`) |
| Java backend / TestNG / Spring Boot | Plain REST `POST /api/workflows/{name}/run` (see `java-client/`) or the Spring AI MCP client |
| CI / cron / terminal | CLI `python -m apichain run order_flow --set productCode=X` (exit code 0/1) |
| Postman / curl | Same REST routes |

---

## 1. Run the chain in context, or as code?

**In context** means the LLM calls `createIndividual`, reads the JSON, copies the `id` into `createCustomer`,
and repeats for each step.

- Every full response body passes through the context window, and the model re-reads the growing transcript
  on every turn. Four steps with 2–5 KB responses comes to roughly 8–20K tokens per run, more with retries.
- The model sometimes copies the wrong ID, picks the wrong field, or "fixes" a payload. The results aren't
  reproducible.
- It's slow: there's an LLM round trip between each pair of HTTP calls.
- It's fine for **exploring** an API you don't know yet, or for debugging a single call.

**As code** means the LLM makes one call, `run_workflow("order_flow", {...})`, and the engine passes the IDs.

- About 300 tokens come back: the step statuses, the extracted IDs, and the outputs. Full bodies go to `runs/*.json`.
- It's deterministic and re-runnable, and it works with **no LLM at all** (CLI, Java, CI).
- Retries for eventual consistency (Kafka/Cassandra lag between steps) are handled in code.

**Recommendation: use both (this repo does).** Expose atomic `call_api` tools so the LLM can explore, and use
`run_workflow` for any chain you already know. If the LLM finds a new chain while exploring, have it **write a
workflow YAML** (it costs tokens once). After that, the chain runs as code for free.

---

## 2. Architecture

```
 apis/*.http  apis/*.curl-in-.http  apis/*.yaml|json (Swagger/OpenAPI)
        \              |                     /
         └──── loaders.py → RequestDef registry ────┐
 config/settings.yaml + .env  →  variables / secrets │
 workflows/*.yaml             →  chain definitions   │
                                                     ▼
                                   engine.py (Registry)
                       render {{vars}} → httpx call → JSONPath extract
                       retry / expect_status / trace to runs/
                                                     │
        ┌───────────────────────┬────────────────────┼──────────────────┐
   server.py (MCP stdio)   server.py (MCP HTTP /mcp)  REST /api/*      __main__.py CLI
     Devin/Claude/Cursor      remote agents            Java backend      CI / terminal
```

**Variable precedence (low → high):** `.http` `@vars` → `settings.yaml` environment → workflow input defaults →
caller inputs → step `with:` → values extracted from earlier steps.
Secrets go only in `.env` or real env vars, referenced as `${DEV_TOKEN}`. Tool results never include them,
and `Authorization`/API-key headers are masked in traces.

**MCP tools exposed:** `list_workflows`, `run_workflow`, `list_apis`, `call_api`, `reload_definitions`, and the
resource `workflow://{name}`.

---

## 3. Quick start

```bash
pip install -r requirements.txt
cp .env.example .env              # put real URLs/tokens here

# try against the bundled mock first
python mock/mock_server.py &      # fake 4 APIs on :9090 (account step fails once to show retry)
export APICHAIN_ENV=local
python -m apichain list
python -m apichain run order_flow --set productCode=UNL-5G
python -m apichain call getOrder --set orderId=ORD-xxxx

# MCP server
python -m apichain.server                                 # stdio
python -m apichain.server --transport http --port 8765    # HTTP: /mcp, /api/*, /health
```

### Plug into clients

**Claude Desktop / Claude Code / Cursor (stdio)**
```json
{
  "mcpServers": {
    "api-chain": {
      "command": "python",
      "args": ["-m", "apichain.server"],
      "cwd": "/path/to/api-chain-mcp",
      "env": { "APICHAIN_ENV": "dev", "APICHAIN_HOME": "/path/to/api-chain-mcp" }
    }
  }
}
```

**Devin:** in Devin's MCP settings, add a custom server. Use either the stdio command above (in Devin's machine,
with the repo cloned there) or the HTTP URL `http://<host>:8765/mcp` if you host it where Devin can reach it.
Add a line to Devin's knowledge/playbook: *"For order creation, call `run_workflow` with `order_flow`. Don't
chain `call_api`."*

**Java:** `java-client/ApiChainClient.java` uses plain `java.net.http`, with no dependencies.

---

## 4. Adding things later (the "drop files in" workflow)

| You have | Do this | Result |
|---|---|---|
| New curl commands | Paste them into any `apis/*.http` under a `### name` + `# @name xxx` line, and replace dynamic parts with `{{var}}` | New `call_api` target |
| REST-client `.http` file | Drop it in `apis/` | Every block becomes a request (`@var = value` lines become defaults) |
| Swagger/OpenAPI (json/yaml) | Drop it in `apis/` | Every operation becomes a request named by `operationId`; path params become `{{param}}`; the JSON payload goes in `variables.body` |
| New chain | Add `workflows/<name>.yaml` | New `run_workflow` target |

Then call `reload_definitions` (MCP), or restart.

**Let the LLM write workflows for you.** Prompt Devin/Claude with:
> Read `workflow://order_flow` as the format example and `list_apis` for available requests. Write
> `workflows/<new>.yaml` that does X → Y → Z, extracting the IDs each next step needs.

### Workflow YAML reference
```yaml
name: order_flow
inputs:  { productCode: {required: true}, quantity: {default: 1}, email: {default: "qa+{{$timestamp}}@x.com"} }
steps:
  - id: individual                # also exposes {{individual.body.<any.field>}} to later steps
    request: createIndividual     # name in .http / operationId in Swagger
    with: { channel: WEB }        # step-level vars
    expect_status: [200, 201]     # default [200, 201, 202]
    retry: { attempts: 3, delay_ms: 1500 }
    extract: { individualId: $.id, loc: "header:Location", firstItem: "$.items[0].id" }
    when: "{{createIndividual}}"  # optional skip condition
    continue_on_error: false
output: { individualId: "{{individualId}}" }
```
Built-ins: `{{$uuid}}`, `{{$timestamp}}`, `{{$timestampMs}}`, `{{$randomInt}}`.

---

## 5. 5-hour plan

| Time | Task |
|---|---|
| 0:00–0:30 | Install, run the mock, and run `order_flow` end to end (already working). |
| 0:30–1:30 | Paste your real 4 curls into `apis/customer-onboarding.http`, replace IDs/names with `{{vars}}`, and put the BASE_URL and token in `.env`/`settings.yaml`. Run `python -m apichain call createIndividual` until each call is green. |
| 1:30–2:15 | Fix the `extract:` JSONPaths in `workflows/order_flow.yaml` to match the real responses (look in `runs/*.json`). Run the full chain against dev. |
| 2:15–3:00 | Auth: if the token is short-lived, add a `getToken` request as step 0 with `extract: {TOKEN: $.access_token}`. Later steps pick it up automatically. |
| 3:00–4:00 | Hook up Devin (stdio or HTTP) and your Java code (REST client). Add the playbook line telling the agent to prefer `run_workflow`. |
| 4:00–5:00 | Buffer: add more APIs and workflows, and deploy the HTTP mode somewhere shared (container or VM) if Devin needs a reachable URL. |

### Hardening later (not needed today)
- Put a reverse proxy/API key in front of HTTP mode, since `/api/*` has no auth of its own. Bind to 127.0.0.1 (the default) until you add one.
- Add an environment allowlist so agents can't run write-chains against prod (e.g. only expose `dev`/`qa` in settings).
- Polling step type (`until: $.status == COMPLETE`) for async order states.
- Dockerfile + one `docker run` for a shared team instance.

## Layout
```
apichain/   template.py (vars + JSONPath) · loaders.py (.http/curl/OpenAPI) · engine.py (executor)
            server.py (MCP + REST) · __main__.py (CLI)
apis/       your .http / swagger files
workflows/  chain definitions
config/     settings.yaml (non-secret, per environment)
mock/       fake APIs for local testing
java-client/ REST caller example
runs/       full traces of each workflow run
```
