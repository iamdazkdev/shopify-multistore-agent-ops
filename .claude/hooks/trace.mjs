#!/usr/bin/env node
// PostToolUse hook. Appends one redacted line per tool call to logs/agent-trace.jsonl
// so every agent session leaves an auditable trail.
import { appendFileSync, mkdirSync, readFileSync } from 'node:fs';
import path from 'node:path';

try {
  const event = JSON.parse(readFileSync(0, 'utf8') || '{}');
  const input = event.tool_input ?? {};
  const redact = (value) =>
    String(value)
      .replace(/shp(at|ss|ca|pa)_[A-Za-z0-9]+/g, 'shp$1_[REDACTED]')
      .replace(/(Bearer\s+)[\w.\-]+/gi, '$1[REDACTED]')
      .replace(/(X-Shopify-Access-Token:?\s*)\S+/gi, '$1[REDACTED]')
      .replace(/(client_secret=)[^&\s]+/gi, '$1[REDACTED]')
      .slice(0, 400);

  const record = {
    ts: new Date().toISOString(),
    session: event.session_id ?? null,
    tool: event.tool_name ?? null,
    subagent: input.subagent_type ?? null,
    target: redact(input.command ?? input.file_path ?? input.path ?? input.pattern ?? input.description ?? ''),
  };

  const dir = path.join(process.env.CLAUDE_PROJECT_DIR ?? event.cwd ?? process.cwd(), 'logs');
  mkdirSync(dir, { recursive: true });
  appendFileSync(path.join(dir, 'agent-trace.jsonl'), JSON.stringify(record) + '\n');
} catch {
  // Tracing must never break the session.
}
process.exit(0);
