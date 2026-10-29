import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

/**
 * Automated Cross-Store QA Auditor.
 * Detects leaked test SKUs, tracking currency mismatches, unrounded VAT prices,
 * and missing delivery-date metafields across all regional storefronts.
 */
export function auditStorefrontsSnapshot(snapshot, allowedEndings = ['.00', '.95', '.99']) {
  const findings = [];

  for (const [market, store] of Object.entries(snapshot.stores || {})) {
    if (store.pixel_tracking_currency && store.pixel_tracking_currency !== store.currency) {
      findings.push({
        severity: 'HIGH',
        market,
        code: 'TRACKING_CURRENCY_MISMATCH',
        message: `Storefront pixel tracking reports "${store.pixel_tracking_currency}" instead of store currency "${store.currency}".`,
      });
    }

    for (const prod of store.products || []) {
      if (prod.status !== 'ACTIVE') continue;

      const hasTestSignal =
        /testing|staging|sandbox/i.test(prod.title || '') ||
        /testing|staging/i.test(prod.handle || '') ||
        (prod.tags || []).some((t) => /testing|staging/i.test(t)) ||
        !prod.sku ||
        prod.sku.toLowerCase() === 'none';

      if (hasTestSignal) {
        findings.push({
          severity: 'CRITICAL',
          market,
          code: 'LEAKED_TEST_PRODUCT',
          message: `Active test/unassigned product "${prod.title}" (handle: ${prod.handle}, sku: ${prod.sku}) is publicly visible.`,
        });
      }

      if (prod.price && !allowedEndings.some((e) => String(prod.price).endsWith(e))) {
        findings.push({
          severity: 'MEDIUM',
          market,
          code: 'UNROUNDED_RETAIL_PRICE',
          message: `SKU ${prod.sku} ("${prod.title}") has unrounded price ${prod.price} ${store.currency}.`,
        });
      }

      if (!prod.estimated_delivery_metafield || prod.estimated_delivery_metafield.trim().length === 0) {
        findings.push({
          severity: 'HIGH',
          market,
          code: 'MISSING_DELIVERY_DATE_METAFIELD',
          message: `SKU ${prod.sku} ("${prod.title}") is missing custom.estimated_delivery_badge metafield.`,
        });
      }
    }
  }

  return {
    passed: findings.length === 0,
    total_stores_checked: Object.keys(snapshot.stores || {}).length,
    findings,
  };
}

export function renderQaMarkdownReport(auditResult, title = 'Cross-Store Automated QA Report') {
  const lines = [
    `# ${title}`,
    ``,
    `- **Timestamp:** ${new Date().toISOString()}`,
    `- **Stores Checked:** ${auditResult.total_stores_checked}`,
    `- **Gate Status:** ${auditResult.passed ? 'PASS (0 regressions)' : `FAIL (${auditResult.findings.length} findings)`}`,
    ``,
  ];

  if (auditResult.findings.length === 0) {
    lines.push(`All regional storefronts passed catalog hygiene, currency conformance, price rounding, and delivery-date metafield checks.`);
  } else {
    lines.push(`| Severity | Market | Rule Code | Details |`);
    lines.push(`| :--- | :--- | :--- | :--- |`);
    for (const f of auditResult.findings) {
      lines.push(`| **${f.severity}** | \`${f.market}\` | \`${f.code}\` | ${f.message} |`);
    }
  }
  return lines.join('\n') + '\n';
}

const isMain = process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url);
if (isMain) {
  const snapIdx = process.argv.indexOf('--snapshot');
  const snapPath = snapIdx !== -1 ? process.argv[snapIdx + 1] : 'data/mock-storefronts.snapshot.json';
  const snapshot = JSON.parse(fs.readFileSync(snapPath, 'utf8'));
  const result = auditStorefrontsSnapshot(snapshot);

  console.log(`\n[QA-GATE] Audited ${result.total_stores_checked} regional storefronts from ${snapPath}`);
  if (result.passed) {
    console.log('[QA-GATE] PASS — Zero cross-store regressions detected.\n');
    process.exit(0);
  } else {
    console.log(`[QA-GATE] FAIL — Detected ${result.findings.length} storefront issue(s):`);
    for (const f of result.findings) {
      console.log(`  - [${f.severity}] [${f.market}] ${f.code}: ${f.message}`);
    }
    console.log('');
    process.exit(1);
  }
}
