@AGENTS.md

## Claude Code wiring

- Subagents: `.claude/agents/` (roster in AGENTS.md §8). Delegate by name, e.g. "use the store-qa-auditor".
- Slash commands:
  - `/ops-change <request>` — full pipeline with the human gate.
  - `/eta-shift <sku or batch> <new window>` — container delay scenario (interview demo).
  - `/launch-market <CODE>` — new-market readiness plan, no writes.
  - `/health-check` — read-only audit of the store and the repo.
  - `/build-store [steps]` — grow the sandbox store toward `schemas/store_buildout.json` with the human gate.
- Hooks (`.claude/settings.json`):
  - `guard-live-writes.mjs` blocks `.env` reads and live-mutating commands unless Claude was started
    with `TT_LIVE_WRITES=1`. Even then the permission prompt still asks.
  - `trace.mjs` appends every tool call (secrets redacted) to `logs/agent-trace.jsonl` — this is the
    agent log artifact for interviews.
- MCP: `shopify-dev-mcp` (`.mcp.json`) for Admin API docs and GraphQL validation. Validate every new
  query or mutation with it before running. If it fails to start (e.g. a broken npx cache), use
  `npm run validate:gql`, which validates against the live schema without executing anything.
- Subagents cannot spawn subagents: the main session orchestrates, subagents do one job and return.
