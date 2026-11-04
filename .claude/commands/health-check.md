---
description: Read-only health audit of the live store and of this repo's data, before a demo or on a schedule
---

Run a read-only health check. No live writes.

1. `store-qa-auditor`: `npm run live:qa` + `npm run live:ops`, plus its extra checks.
2. `catalog-data-steward`: repo vs live drift (missing/extra products, metafield gaps, stock mismatches).
3. `npm test`.
4. Re-check the open items in AGENTS.md §7 and mark which are still present (file:line evidence).

Output one table: Area | Status (OK/WARN/FAIL) | Evidence | Next action — then a 3-sentence summary
in plain English that DazkDev could say out loud in an interview.
