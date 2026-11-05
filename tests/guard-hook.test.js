// The PreToolUse hook is the "script decides what's allowed" layer: test it like code.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import path from 'node:path';

const hook = path.resolve(import.meta.dirname, '../.claude/hooks/guard-live-writes.mjs');

function decide(command, env = {}) {
  const r = spawnSync('node', [hook], {
    input: JSON.stringify({ tool_input: { command } }),
    env: { ...process.env, TT_LIVE_WRITES: '', ...env },
  });
  return r.status === 2 ? 'BLOCK' : 'ALLOW';
}

const cases = [
  ['python3 src/store_audit.py', 'ALLOW'],
  ['python3 src/gated_eta_update.py --sku X --qty 5 --dry-run', 'ALLOW'],
  ['python3 src/gated_eta_update.py --sku X --qty 5', 'BLOCK'],
  ['python3 src/gated_eta_update.py --rollback eta_1', 'BLOCK'],
  ['python3 src/translate_product.py --handle h --locale de --title T', 'ALLOW'],
  ['python3 src/translate_product.py --handle h --locale de --title T --reviewer Anna', 'BLOCK'],
  ['python3 src/store_build.py', 'ALLOW'],
  ['python3 src/store_build.py --apply', 'BLOCK'],
  ['npm run live:3pl:apply', 'BLOCK'],
  ['grep -rn "mutation" src', 'ALLOW'],
  ['python3 -c \'q="mutation X { productDelete(input:{}) { id } }"\'', 'BLOCK'],
  ['cat .env', 'BLOCK'],
];

for (const [command, expected] of cases) {
  test(`${expected}: ${command}`, () => assert.equal(decide(command), expected));
}

test('a human-started session with TT_LIVE_WRITES=1 may run gated writes', () => {
  assert.equal(decide('python3 src/store_build.py --apply', { TT_LIVE_WRITES: '1' }), 'ALLOW');
});

test('.env stays blocked even with TT_LIVE_WRITES=1', () => {
  assert.equal(decide('cat .env', { TT_LIVE_WRITES: '1' }), 'BLOCK');
});
