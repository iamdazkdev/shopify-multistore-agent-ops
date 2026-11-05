---
description: Plan a new international market launch (readiness checklist and change plan only — no writes)
argument-hint: <market code, e.g. NL, SG, KSA>
---

Prepare the launch plan for market **$ARGUMENTS**. Goal: opening the next market is routine, not a project.

1. `compliance-checker`: currency, consumer tax rate (from an official source), price-display rules,
   statutory pages, registrations needed (owner + status UNKNOWN unless told).
2. `catalog-data-steward`: which SKUs can be served from which hub, lead time + customs buffer
   assumptions, gaps in the manifest for this market.
3. `localization-reviewer`: locale(s) needed, terms to add to the glossary, strings to translate and queue.
4. `shopify-graphql-engineer`: the store-side setup as a plan — store or Market, currency, tax-inclusive
   pricing, shipping zones and rates, payment methods, metafield values, navigation, theme sections,
   and the `schemas/stores.config.json` block to add. Point out what must be done in Shopify admin by a
   human (payment providers, domains, tax registrations) vs what an agent can do through the API.
5. Write `artifacts/<date>-launch-<code>.plan.json` and a checklist table:
   Workstream | Task | Owner (agent / human / legal / finance) | Blocking? | Status.
6. `ops-reporter`: a 150-word launch brief with the critical path and the decisions Rodrigo must make.

Do not run `market:launch:*` or any other live command in this flow.
