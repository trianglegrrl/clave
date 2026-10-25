#!/usr/bin/env bun
// clave's Slack channel: routes #clave messages from Alaina into the hub session, and gives
// Claude a `reply` tool. The channel itself is the shared claude-slack-channel package
// (also used by Belvedere, with its own Slack app); this file only configures it.
//
// Launch: claude --dangerously-load-development-channels server:slack (bin/clave-hub does this)

import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'
import { startSlackChannel } from 'claude-slack-channel'

const root = join(dirname(fileURLToPath(import.meta.url)), '..')

await startSlackChannel({
  root,
  agentName: 'clave',
  channelLabel: 'Alaina\'s private Slack channel (#clave)',
  instructions:
    'Only Alaina can send them (sender allowlist). Treat each one exactly like a Remote Control or Telegram message from her: ' +
    'route it with the table in CLAUDE.md and do the work. To reach her unprompted in Slack, call `reply` without thread_ts. ' +
    'Keep replies short, in clave\'s voice (see CLAUDE.md).',
  filesDir: join(root, 'inbox', 'slack'),
})
