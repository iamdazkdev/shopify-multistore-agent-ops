import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { parseAndValidateCsv, loadStoresConfig } from '../src/lib/validator.js';
import { buildLocalizedStoreUpdate } from '../src/lib/localizer.js';
import { auditStorefrontsSnapshot } from '../src/qa-audit.js';

const storesConfig = loadStoresConfig('schemas/stores.config.json');

test('validates clean multi-store warehouse ETA CSV feed without errors', () => {
  const csv = fs.readFileSync('data/warehouse-eta-sheet.csv', 'utf8');
  const res = parseAndValidateCsv(csv, storesConfig);
  assert.equal(res.valid, true);
  assert.equal(res.records.length, 9);
});

test('rejects backorder SKU missing container_eta and unrounded VAT prices', () => {
  const badCsv = [
    'sku,product_handle,title_en,market_code,on_hand_qty,container_eta,price_local,backorder_allowed',
    'MOD-TBL-OAK-300,extendable-dining-table-oak,Extendable Dining Table,UK,0,,1428.57,true',
  ].join('\n');
  const res = parseAndValidateCsv(badCsv, storesConfig);
  assert.equal(res.valid, false);
  assert.equal(res.errors.length, 2);
});

test('localizes German backorder delivery date with customs buffer and DD.MM.YYYY format', async () => {
  const deStore = storesConfig.stores.find((s) => s.market_code === 'DE');
  const update = await buildLocalizedStoreUpdate(
    {
      sku: 'MOD-TBL-OAK-300',
      product_handle: 'extendable-dining-table-oak',
      title_en: 'Extendable Dining Table (Smoked Oak)',
      market_code: 'DE',
      on_hand_qty: 0,
      container_eta: '2026-11-24',
      price_local: '1699.00',
      backorder_allowed: true,
    },
    deStore
  );

  assert.equal(update.localized_title, 'Ausziehbarer Esstisch (Räuchereiche)');
  assert.equal(update.estimated_delivery_iso, '2026-11-29');
  assert.match(update.delivery_badge_metafield, /Vorbestellung \| Voraussichtliche Lieferung bis 29\.11\.2026/);
});

test('cross-store QA auditor catches leaked test products, hardcoded USD tracking, and unrounded prices', () => {
  const baseline = JSON.parse(fs.readFileSync('data/mock-storefronts.snapshot.json', 'utf8'));
  const report = auditStorefrontsSnapshot(baseline);
  assert.equal(report.passed, false);
  const codes = report.findings.map((f) => f.code);
  assert.ok(codes.includes('LEAKED_TEST_PRODUCT'));
  assert.ok(codes.includes('TRACKING_CURRENCY_MISMATCH'));
  assert.ok(codes.includes('UNROUNDED_RETAIL_PRICE'));
});
