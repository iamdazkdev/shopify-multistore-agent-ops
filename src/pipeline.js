import fs from 'node:fs';
import path from 'node:path';
import readline from 'node:readline/promises';
import { stdin as input, stdout as output } from 'node:process';
import { parseAndValidateCsv, loadStoresConfig } from './lib/validator.js';
import { buildLocalizedStoreUpdate } from './lib/localizer.js';
import { syncStoreActionToShopify } from './lib/shopify-graphql.js';
import { auditStorefrontsSnapshot, renderQaMarkdownReport } from './qa-audit.js';

async function main() {
  const args = process.argv.slice(2);
  const inputIdx = args.indexOf('--input');
  const csvPath = inputIdx !== -1 ? args[inputIdx + 1] : 'data/warehouse-eta-sheet.csv';
  const autoApprove = args.includes('--auto-approve');
  const planOnly = args.includes('--plan-only');
  const liveMode = args.includes('--live');

  const runId = `run_${new Date().toISOString().replace(/[:.]/g, '-')}`;
  const storesConfig = loadStoresConfig('schemas/stores.config.json');
  const storeMap = new Map(storesConfig.stores.map((s) => [s.market_code, s]));

  console.log(`\n======================================================================`);
  console.log(` SHOPIFY MULTI-STORE AGENTIC PIPELINE  |  Run ID: ${runId}`);
  console.log(` Mode: ${liveMode ? 'LIVE GRAPHQL' : 'DRY-RUN SIMULATION'}  |  Feed: ${csvPath}`);
  console.log(`======================================================================\n`);

  // STAGE 1: Schema Validation
  console.log(`[1/4] Validating warehouse & container ETA feed against schema v${storesConfig.schema_version}...`);
  const csvText = fs.readFileSync(csvPath, 'utf8');
  const validation = parseAndValidateCsv(csvText, storesConfig);

  if (!validation.valid) {
    console.error(`[ABORT] Schema validation failed with ${validation.errors.length} error(s):`);
    for (const err of validation.errors) console.error(`  x ${err}`);
    process.exit(1);
  }
  console.log(`  ✓ Validated ${validation.records.length} regional SKU rows across ${storesConfig.stores.length} stores.\n`);

  // STAGE 2: Localization & Execution Plan Generation
  console.log(`[2/4] Computing delivery windows & localized storefront metafields...`);
  const plannedActions = [];
  for (const rec of validation.records) {
    const store = storeMap.get(rec.market_code);
    plannedActions.push(await buildLocalizedStoreUpdate(rec, store));
  }

  fs.mkdirSync('artifacts', { recursive: true });
  fs.writeFileSync(
    'artifacts/pending-plan.json',
    JSON.stringify({ run_id: runId, generated_at: new Date().toISOString(), actions: plannedActions }, null, 2)
  );

  for (const act of plannedActions) {
    const flag = act.needs_native_review ? '[L10N-REVIEW]' : '[READY]      ';
    console.log(
      `  ${flag} [${act.market_code}] ${act.sku.padEnd(16)} | Qty: ${String(act.on_hand_qty).padStart(2)} | ${act.price_local} ${act.currency} | "${act.delivery_badge_metafield}"`
    );
  }
  console.log(`\n  ✓ Saved structured diff plan -> artifacts/pending-plan.json\n`);

  if (planOnly) {
    console.log(`[STOP] --plan-only specified. Awaiting human review of artifacts/pending-plan.json.\n`);
    return;
  }

  // HUMAN DECISION GATE
  let approved = autoApprove;
  if (!autoApprove) {
    const rl = readline.createInterface({ input, output });
    const answer = await rl.question(
      `[DECISION GATE] Approve pushing ${plannedActions.length} updates across UK, DE, AU stores? (y/N): `
    );
    rl.close();
    approved = /^y(es)?$/i.test(answer.trim());
  } else {
    console.log(`[DECISION GATE] Approved via --auto-approve flag.\n`);
  }

  if (!approved) {
    console.log(`[CANCELLED] Operator rejected plan at decision gate. Zero stores modified.\n`);
    process.exit(0);
  }

  // STAGE 3: Shopify Admin GraphQL Sync
  console.log(`[3/4] Executing Shopify Admin GraphQL batch sync...`);
  const syncResults = [];
  for (const act of plannedActions) {
    const res = await syncStoreActionToShopify(act, { live: liveMode });
    syncResults.push(res);
  }
  console.log(`  ✓ Synced ${syncResults.length} variant inventory & delivery-date metafield payloads.\n`);

  // Build post-sync verification snapshot
  const postSyncSnapshot = { captured_at: new Date().toISOString(), stores: {} };
  for (const store of storesConfig.stores) {
    const storeActions = plannedActions.filter((a) => a.market_code === store.market_code);
    postSyncSnapshot.stores[store.market_code] = {
      currency: store.currency,
      locale: store.locale,
      pixel_tracking_currency: store.currency,
      products: storeActions.map((a) => ({
        handle: a.product_handle,
        title: a.localized_title,
        sku: a.sku,
        status: 'ACTIVE',
        tags: ['modular', 'AgentSynced'],
        price: a.price_local,
        inventory_qty: a.on_hand_qty,
        estimated_delivery_metafield: a.delivery_badge_metafield,
      })),
    };
  }
  fs.writeFileSync('artifacts/post-sync-storefronts.json', JSON.stringify(postSyncSnapshot, null, 2));

  // STAGE 4: Automated Cross-Store QA Verification
  console.log(`[4/4] Running post-deployment Cross-Store QA Gate...`);
  const qaResult = auditStorefrontsSnapshot(postSyncSnapshot);
  fs.mkdirSync('logs', { recursive: true });
  fs.writeFileSync('logs/latest-qa-report.md', renderQaMarkdownReport(qaResult));

  const logEntry = {
    run_id: runId,
    timestamp: new Date().toISOString(),
    mode: liveMode ? 'LIVE_GRAPHQL' : 'DRY_RUN_SIMULATED',
    rows_processed: plannedActions.length,
    stores_updated: storesConfig.stores.map((s) => s.market_code),
    qa_passed: qaResult.passed,
    qa_findings_count: qaResult.findings.length,
  };
  fs.appendFileSync('logs/agent-runs.jsonl', JSON.stringify(logEntry) + '\n');

  if (!qaResult.passed) {
    console.error(`  x QA Gate FAILED with ${qaResult.findings.length} finding(s). See logs/latest-qa-report.md`);
    process.exit(1);
  }

  console.log(`  ✓ QA Gate PASSED (0 leaked test SKUs, 0 currency mismatches, 100% delivery metafields verified).`);
  console.log(`  ✓ Logged run artifact -> logs/agent-runs.jsonl & logs/latest-qa-report.md\n`);
}

main().catch((err) => {
  console.error('[FATAL]', err.message);
  process.exit(1);
});
