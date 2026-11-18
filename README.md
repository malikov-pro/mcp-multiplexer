# mcp-multiplexer

A small, strict **MCP multiplexer**: one MCP endpoint over many equivalent MCP
servers. The first (reference) server defines the tool contract; members with a
different tool set, schema or version are hidden. Every tool gets a required
selector parameter (`base` by default) whose enum contains only compatible
members.

This is a reference implementation for the MCPHub feature idea
[“first-class Multiplexer”](https://github.com/samanhappy/mcphub) — and a
working service for 1C data/admin endpoints.

## Why

The same MCP server is often deployed many times: one per 1C base, store, tenant
or environment. Prefixing gives you `base-a-execute_query`,
`base-b-execute_query`, … and N copies of every schema in the model context.
This service gives you exactly one `execute_query` with:

```json
"base": { "type": "string", "enum": ["rt", "ut", "cost"] }
```

The model chooses a base; the multiplexer routes the call. Optionally, the
read-only tools can run on all compatible bases in one request (`"base": "all"`).

## Strict model

- `reference` (default: the first base) defines the tool names, input schemas and
  descriptions.
- Another base is exposed only when **all** of these match:
  - exact tool-name set (no extra, no missing tools);
  - exact normalized `inputSchema` for every tool;
  - exact upstream version from `initialize` (`version: "ignore"` disables this).
- An incompatible base is hidden completely: its tools do not appear and its
  value is not in the enum. `/health` shows the reason.
- The same tool name may behave differently on different bases (different data,
  different rows). That is expected: compatibility is about the contract, not
  the result. The selector chooses the implementation/data set.
- If the reference is unavailable at startup, the service fails to start; it
  never silently switches the reference to another base.
- `base` is required even when only one member is compatible, so a member
  recovering does not change the client-visible schema.

## Quick start

```bash
uv venv --python 3.12
. .venv/bin/activate
uv pip install -e '.[dev]'

cp config/config.example.json config/config.json
# edit config/config.json: URLs, users, passwords, reference, fanOut

MCP_CONFIG=config/config.json python -m mcp_multiplexer
# or:
# MCP_CONFIG=config/config.json uvicorn mcp_multiplexer.app:create_app_from_env \
#   --factory --host 0.0.0.0 --port 8010
```

Check:

```bash
curl -s http://127.0.0.1:8010/health | jq
```

MCP endpoint: `http://127.0.0.1:8010/mcp` (Streamable HTTP, stateless, JSON
responses).

## Docker

```bash
mkdir -p config
cp config/config.example.json config/config.json
# edit config/config.json

docker compose up -d --build
curl -s http://127.0.0.1:8010/health
```

The container expects `config/config.json` mounted read-only at
`/config/config.json`; it joins the external `mcpnet` network. No port is
published by default; MCPHub reaches it as `http://mcp-multiplexer:8010/mcp`.

## Config

```json
{
  "name": "data-farm",
  "groupBy": "base",
  "reference": "data_ut",
  "compatibility": {
    "tools": "exact",
    "schemas": "exact",
    "version": "exact"
  },
  "onIncompatible": "hide",
  "fanOut": {
    "enabled": true,
    "tools": ["execute_query", "validate_query"],
    "maxConcurrency": 4,
    "timeoutMs": 30000
  },
  "toolsEnabled": [],
  "bases": [
    {
      "name": "data_ut",
      "value": "ut",
      "url": "http://onec-ws:8080/ut_kz",
      "username": "<service user>",
      "password": "<password>",
      "serviceRoot": "mcp",
      "maxConcurrent": 4
    },
    {
      "name": "data_ut_sebestoimost",
      "value": "cost",
      "url": "http://onec-ws:8080/ut_sebestoimost",
      "username": "<service user>",
      "password": "<password>",
      "serviceRoot": "mcp",
      "maxConcurrent": 2
    }
  ]
}
```

Base fields:

| Field | Meaning |
|---|---|
| `name` | Unique server name (also used for health/logs) |
| `value` | Selector value in the enum; defaults to `name`; cannot be `all` |
| `url` | Publication root, without `/hs/...` |
| `rpcUrl` | Full JSON-RPC URL, overrides `url`/`serviceRoot` (APISIX use case) |
| `serviceRoot` | 1C HTTP service root, default `mcp`; `admin` for `/admin` |
| `username`/`password` | Basic auth to the publication |
| `headers` | Custom headers, e.g. `X-API-Key` for APISIX |
| `maxConcurrent` | Per-base request limit |

Top-level fields:

| Field | Meaning |
|---|---|
| `groupBy` | Name of the synthetic selector parameter |
| `reference` | Canonical base; default first |
| `compatibility` | `tools`/`schemas`/`version`; `version` may be `ignore` |
| `onIncompatible` | `hide` (default) or `fail` |
| `fanOut` | Optional parallel read-only calls |
| `toolsEnabled` | Optional allowlist of reference tool names |

`config/config.admin.example.json` shows the APISIX-token variant.

## APISIX token auth

If the 1C endpoints are behind APISIX with `key-auth`, do not point the
multiplexer at `onec-ws`. Use the public APISIX URL and pass the consumer key in
a header:

```json
{
  "name": "admin_forum",
  "value": "forum",
  "rpcUrl": "https://gateway.example.com/admin/forum/rpc",
  "headers": { "X-API-Key": "<apisix consumer admin key>" },
  "maxConcurrent": 2
}
```

APISIX checks `X-API-Key` and injects `Authorization: Basic` from OpenBao. The
multiplexer does not need the per-base 1C passwords.

Use the public FQDN (`https://gateway.example.com`), not the internal APISIX port:
APISIX route/consumer `ip-restriction` sees the NPM source address, which is in
the allowed `10.20.0.0/16`; a direct docker-network request would be rejected.

## Fan-out

`fanOut.tools` is an explicit allowlist. For those tools, the selector enum also
contains `"all"`:

```json
{ "name": "execute_query", "arguments": { "query": "SELECT 1", "base": "all" } }
```

The multiplexer calls every compatible member in parallel, applies
`maxConcurrency` and `timeoutMs`, and returns a JSON payload with per-target
results:

```json
{
  "summary": { "requested": 3, "succeeded": 2, "failed": 1 },
  "results": [
    { "target": "rt", "ok": true,  "result": { "content": [ ... ] } },
    { "target": "ut", "ok": true,  "result": { "content": [ ... ] } },
    { "target": "cost", "ok": false, "error": "timeout after 30000 ms" }
  ]
}
```

Writes are never fanned out: only explicitly listed tools are eligible, and a
single target is still required for everything else.

## Deploy on the mcphub host

The service belongs on the same Docker host as MCPHub and `onec-ws`, in the
`mcpnet` network. It publishes no host port; MCPHub reaches it by container
name.

### Data multiplexer

```bash
cd ~/deploy
git clone https://github.com/malikov-pro/mcp-multiplexer.git
cd mcp-multiplexer

cp config/config.example.json config/config.json
$EDITOR config/config.json          # URLs, service users, passwords
chmod 600 config/config.json

docker network inspect mcpnet >/dev/null
docker compose up -d --build
docker compose ps
docker logs -f mcp-multiplexer
```

Health from inside `mcpnet` (the container does not publish a host port):

```bash
docker run --rm --network mcpnet curlimages/curl:8.11.1 \
  -s http://mcp-multiplexer:8010/health
```

### Admin multiplexer

A multiplexer is one homogeneous tool family. Run a second container for
`/admin` instead of mixing data and admin tools:

```bash
cp config/config.admin.example.json config/config.admin.json
$EDITOR config/config.admin.json    # APISIX rpcUrl + X-API-Key
chmod 600 config/config.admin.json

docker compose -f compose.admin.yaml up -d --build
docker run --rm --network mcpnet curlimages/curl:8.11.1 \
  -s http://mcp-multiplexer-admin:8010/health
```

In `config.admin.example.json` the reference is `data_rt_test` — the test base's
universal `/admin` endpoint (`https://gateway.example.com/admin/rpc`), and
`admin_forum` is the forum store. Member names are local to the config; both
must expose the same admin tools and version for strict compatibility.

### MCPHub

Add one server per multiplexer:

| Field | Value |
|---|---|
| Name | `data-farm` / `admin-farm` |
| Type | Streamable HTTP |
| URL | `http://mcp-multiplexer:8010/mcp` / `http://mcp-multiplexer-admin:8010/mcp` |

Put the server in the matching group and keep the per-base servers out of that
group. MCPHub will prefix the tools with the server name, while the multiplexer
keeps the selector parameter.

### Update / restart

```bash
cd ~/deploy/mcp-multiplexer
git pull --ff-only
docker compose up -d --build                 # data
docker compose -f compose.admin.yaml up -d --build  # admin, if used
```

Config-only change: `docker compose restart mcp-multiplexer` (and the admin
equivalent). The config is read at startup.

### Common failures

| Symptom | Cause |
|---|---|
| container restarts, log says `reference base ... is unavailable` | wrong URL/credentials, or reference base is down |
| APISIX returns 401 | wrong `X-API-Key` in `headers` |
| APISIX returns 403 | source IP is outside the route/consumer whitelist; use `https://gateway.example.com`, not the internal APISIX port |
| `/health` shows a member `incompatible` | tool set, schema or version differs from the reference; see `reason` |
| MCPHub cannot connect | container is not on `mcpnet`, or URL uses a host port instead of the container name |

## MCPHub

Register this service as one MCP server:

- Name: `data-farm` (or `admin-farm`)
- URL: `http://mcp-multiplexer:8010/mcp`
- Transport: Streamable HTTP

Put it in a group (for example `data`) and do not expose the individual
per-base servers in the same group. MCPHub will prefix the tools with the
server name (`data-farm-execute_query`), while this service keeps the selector
parameter and the strict contract.

The same object model is described in
[`docs/mcphub-proposal.md`](docs/mcphub-proposal.md): a first-class `Multiplexer`
object, usable as a group member and shared with users. This repo is the
executable reference for that proposal.

## Relation to `router-1c-mcp`

[`router-1c-mcp`](https://github.com/Regsorm/router-1c-mcp) is the closest prior
art: one process for many 1C bases, tools merged by name, a `base` parameter,
per-base Basic auth and concurrency. This project differs deliberately:

- **strict reference** instead of “oldest extension version wins”;
- **APISIX token support** through `headers` and `rpcUrl` (the Rust router only
  supports Basic auth and builds `/hs/<service_root>/rpc` itself);
- **Python** and a small codebase focused on `tools/list` + `tools/call`;
- **optional fan-out** for read-only tools;
- no artifact upload/download, PID lock or retry loop.

## Limitations

- One multiplexer is one homogeneous tool family: data servers together, admin
  servers together. Do not mix unrelated tool sets.
- The reference must be reachable at startup; incompatible members are hidden
  and reported in `/health`.
- Catalog refresh is done at startup; restart after changing manifests/tools.
- No artifact download/upload, no write fan-out, no nested multiplexers.
- No built-in auth for the multiplexer endpoint; put it behind APISIX/MCPHub or
  keep it on the internal `mcpnet`.
- This is a reference implementation, not a drop-in replacement for the full
  `router-1c-mcp` feature set.

## Tests

```bash
. .venv/bin/activate
pytest -q
ruff check src tests
```

The tests cover strict compatibility, selector injection, hidden members,
fan-out, APISIX headers/`rpcUrl`, Basic auth, JSON-RPC errors and the MCP
`initialize`/`tools/list`/`tools/call` flow with a mocked upstream.

## Prior art

- [router-1c-mcp](https://github.com/Regsorm/router-1c-mcp) — tool matrix and
  `base` selector for 1C.
- [MCPHub](https://github.com/samanhappy/mcphub) — groups, namespacing and Smart
  Routing; the multiplexer is intended to sit in front of it.
- [reaatech/mcp-gateway](https://github.com/reaatech/mcp-gateway) — fan-out
  aggregation vocabulary.

See `NOTICE` for attribution details.

## License

MIT. See `LICENSE`.
