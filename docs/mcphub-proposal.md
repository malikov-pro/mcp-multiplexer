# Proposal: a first-class Multiplexer in MCPHub

Status: reference implementation in this repository
([`malikov-pro/mcp-multiplexer`](https://github.com/malikov-pro/mcp-multiplexer)).

## Problem

MCPHub already multiplexes many servers behind `/mcp/<group>`, but it only
namespaces tools. When the same server is deployed many times with the same tool
set (one per tenant, base, store or environment), every tool appears once per
member:

```
base-a-execute_query
base-b-execute_query
base-c-execute_query
```

This bloats tool schemas in the model context and leaves no way to run one
read-only tool across all members in a single call.

## Object

Add a new resource, **Multiplexer**, alongside Server and Group. It is a virtual
MCP endpoint that merges equivalent tools and exposes one selector parameter.

```json
{
  "name": "data-farm",
  "description": "Data tools across equivalent 1C bases",
  "groupBy": "base",
  "reference": "data_rt_test",
  "compatibility": {
    "tools": "exact",
    "schemas": "exact",
    "version": "exact"
  },
  "onIncompatible": "hide",
  "members": [
    { "server": "data_rt_test", "value": "rt",   "label": "RT test" },
    { "server": "data_ut",      "value": "ut",   "label": "UT" },
    { "server": "data_cost",    "value": "cost", "label": "UT cost" }
  ],
  "fanOut": {
    "enabled": true,
    "tools": ["execute_query", "validate_query", "get_metadata_structure"],
    "maxConcurrency": 4,
    "timeoutMs": 30000
  },
  "visibility": "private",
  "sharedWithUsers": [],
  "sharedWithUserGroups": []
}
```

- `groupBy` is the synthetic input parameter clients use to choose a member.
- `reference` (default: first member) defines the tool names, schemas and
  descriptions. It is the contract.
- `compatibility` is strict by default: exact tool set, exact schemas, exact
  upstream version. `version: "ignore"` is available for servers that do not
  report a version.
- `onIncompatible` is `hide` (default) or `fail`.
- Multiplexers do not nest in v1; members are servers only.

## Behaviour

### Listing

1. Read the reference member's tools. This is the only tool set exposed.
2. For every other member, compare, after its own tool selection:
   - exact tool-name set;
   - exact normalized `inputSchema` for every tool;
   - exact upstream version from `initialize`.
3. A compatible member is added to the `groupBy` enum for every reference tool.
4. An incompatible member is hidden completely: its tools do not appear, its
   value is not in the enum, and the UI/health reports the reason (`tool set
   differs`, `schema differs`, `version 1.6.2 vs 1.6.3`, `unreachable`).
5. If the reference is unavailable, the multiplexer is degraded. Do not switch
   the reference automatically; use the last known snapshot or list no tools.
6. `groupBy` remains required even with one compatible member, so a member
   recovering does not change the client-visible schema.
7. Tool names are projected as `<multiplexer><separator><tool>`; group
   prefixing applies as usual. Descriptions come from the reference.

The same tool name may behave differently on different members. That is normal:
compatibility is about the contract, not about identical results. The selector
chooses the implementation/data set.

### Calls

- Selector is required; resolve the member, strip the selector, forward.
- Unknown selector is rejected before any network call with the valid values.
- Writes stay single-target.
- `all` is allowed only for tools explicitly listed in `fanOut.tools`.
- Fan-out runs compatible members in parallel with `maxConcurrency` and
  `timeoutMs`; one failed member does not fail the whole call.
- Suggested result shape:

```json
{
  "summary": { "requested": 3, "succeeded": 2, "failed": 1 },
  "results": [
    { "target": "rt", "ok": true, "result": { "content": [ ... ] } },
    { "target": "ut", "ok": true, "result": { "content": [ ... ] } },
    { "target": "cost", "ok": false, "error": "timeout after 30000 ms" }
  ]
}
```

### Groups and users

- A group member can be a server or a multiplexer:

```json
"members": [
  { "kind": "server",      "name": "data_rt_test" },
  { "kind": "multiplexer", "name": "data-farm", "alias": "data" }
]
```

- A multiplexer is shareable like a server: `visibility`, `sharedWithUsers`,
  `sharedWithUserGroups`, and a direct route `/mcp/<multiplexer>`.
- Multiplexer names share the endpoint namespace with servers and groups;
  collisions are rejected.
- Pick and document access semantics. Recommended: sharing a multiplexer grants
  its merged surface as a capability; tool policies can still deny a specific
  member/tool. The alternative (caller needs access to every member) matches
  today’s group rule but makes the object much less useful.
- Tool policies and activity logs need a multiplexer dimension and the resolved
  member target. Fan-out checks each member separately.
- Personal OAuth: either restrict v1 members to servers with shared/system
  credentials, or resolve each member with the caller's credentials and report
  per-member auth errors. Never silently use the multiplexer owner's credentials.

## Mapping to MCPHub

- New `Multiplexer` entity/table; migrate `Group.servers` to a
  server/multiplexer member union.
- Runtime: `MultiplexerInfo` (or a virtual `ServerInfo`) with a
  `MultiplexerClient` implementing `listTools`/`callTool`; it skips
  transport/spawn and dispatches to member clients. This reuses group listing,
  auth, policies and logs.
- `mcpTools.ts`: reference projection in `listGroupTools`; compatibility checks
  and selector resolution in `resolveToolInGroup`/`resolveTool`.
- `mcpService.ts`: selector routing and fan-out in
  `handleCallToolRequestImpl`, with per-member policy checks and activity logs.
- API: CRUD `/api/multiplexers`; group config accepts multiplexer members; user
  sharing accepts multiplexers.
- UI: Multiplexers page (members, reference, groupBy, compatibility, per-member
  status/reason, fan-out, health), member picker in the group editor, sharing in
  user settings.
- Health: aggregate status with per-member detail. Compatibility is evaluated
  from the last known snapshot; a member that becomes unreachable is not removed
  from the enum, so the client-visible schema is stable and calls return a
  per-target error.

## Prior art

- [router-1c-mcp](https://github.com/Regsorm/router-1c-mcp) — tool→base matrix and
  routing for 1C; this proposal is stricter (reference defines the contract).
- [reaatech/mcp-gateway](https://github.com/reaatech/mcp-gateway) — fan-out
  all-wait vocabulary.
- [Zilla AI Gateway Virtual Server](https://docs.aklivity.io/latest/ai-gateway/mcp-gateway/multi-server-aggregation)
  — tools/list fan-out and deduplication.
- [MetaMCP](https://github.com/metatool-ai/metamcp) — namespaces and tool
  overrides; confirms namespacing alone does not solve this.

## Reference implementation

This repository implements the strict model as a standalone Python service:

- `tools/list` and `tools/call` over stateless Streamable HTTP;
- strict reference/compatibility and hidden incompatible members;
- APISIX `X-API-Key` + full `rpcUrl`;
- optional read-only fan-out;
- tests, Docker and examples.

It is intended as a working reference for the MCPHub feature, not as a
replacement for MCPHub core.
