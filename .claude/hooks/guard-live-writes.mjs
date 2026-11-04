#!/usr/bin/env node
// PreToolUse hook (Bash). Exit 2 = block the tool call and tell Claude why.
// 1) Never let an agent read .env (credentials).
// 2) Block commands that mutate the live Shopify store unless the human started this
//    session with TT_LIVE_WRITES=1. Permission rules in settings.json still prompt on top.
import { readFileSync } from 'node:fs';

let event = {};
try {
  event = JSON.parse(readFileSync(0, 'utf8') || '{}');
} catch {
  process.exit(0);
}
const cmd = String(event?.tool_input?.command ?? '');

const READS_ENV = /(^|[\s'"=\/<])\.env(?=$|[\s'";|&)])/;

const LIVE_WRITES = [
  /--live\b/,
  /npm\s+run\s+(live:enterprise|live:jd-complete|live:3pl:apply|market:launch|demo:eta-shift|demo:reset)\b/,
  /live-ops-cli\.py\s+sync-3pl\b.*--apply\b/,
  /python3?\s+\S*src\/(enterprise-pod-suite|role3-complete-jd-engine|live-demo-trainer)\.py/,
  /shopify\s+theme\s+(push|publish|delete)\b/,
  /shopify\s+app\s+deploy\b/,
  /myshopify\.com\/admin\/api\/\S*graphql\.json[\s\S]*mutation/i,
];

if (READS_ENV.test(cmd)) {
  console.error('BLOCKED: reading .env is not allowed for agents. Use .env.example to learn variable names.');
  process.exit(2);
}

if (LIVE_WRITES.some((re) => re.test(cmd)) && process.env.TT_LIVE_WRITES !== '1') {
  console.error(
    'BLOCKED by the human decision gate: this command changes the live Shopify store. ' +
      'Show the approved plan and the exact command, and ask DazkDev to run it in their terminal ' +
      '(or restart Claude with TT_LIVE_WRITES=1 after approval).'
  );
  process.exit(2);
}

process.exit(0);
