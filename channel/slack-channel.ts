#!/usr/bin/env bun
// Custom Claude Code channel for clave: routes messages from #clave into the hub
// session, and gives Claude a `reply` tool to answer in that channel.
// Uses Slack Socket Mode (outbound websocket), so razorback needs no public URL.
// Adapted from ~/family-food/channel/slack-channel.ts (Belvedere); uses its own Slack app (Clave).
//
// Launch: claude --dangerously-load-development-channels server:slack (bin/clave-hub does this)
// NOTE: stdout is the MCP transport. Never console.log; use console.error.

import { Server } from '@modelcontextprotocol/sdk/server/index.js'
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js'
import { ListToolsRequestSchema, CallToolRequestSchema } from '@modelcontextprotocol/sdk/types.js'
import { SocketModeClient } from '@slack/socket-mode'
import { WebClient } from '@slack/web-api'
import { z } from 'zod'
import { readFileSync, existsSync } from 'node:fs'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

// ---- config (.env in the project root) ----
const root = join(dirname(fileURLToPath(import.meta.url)), '..')
const envPath = join(root, '.env')
if (existsSync(envPath)) {
  for (const line of readFileSync(envPath, 'utf8').split('\n')) {
    const m = line.match(/^\s*([A-Z0-9_]+)\s*=\s*(.*?)\s*$/)
    if (m && !process.env[m[1]]) process.env[m[1]] = m[2].replace(/^["']|["']$/g, '')
  }
}
function need(k: string): string {
  const v = process.env[k]
  if (!v) { console.error(`[slack-channel] missing ${k} in .env`); process.exit(1) }
  return v
}
const BOT_TOKEN = need('SLACK_BOT_TOKEN')        // xoxb-...
const APP_TOKEN = need('SLACK_APP_TOKEN')        // xapp-... (connections:write)
const CHANNEL = need('SLACK_CHANNEL_ID')         // C... id of #clave
const ALLOWED = new Set(need('SLACK_ALLOWED_USERS').split(',').map(s => s.trim()).filter(Boolean)) // U... ids

// Slack's default logger writes to stdout, which would corrupt MCP. Send everything to stderr.
const quiet = {
  debug() {}, info() {},
  warn: (...a: unknown[]) => console.error('[slack-channel]', ...a),
  error: (...a: unknown[]) => console.error('[slack-channel]', ...a),
  setLevel() {}, getLevel() { return 'error' as any }, setName() {},
}

const web = new WebClient(BOT_TOKEN, { logger: quiet as any })
const names = new Map<string, string>()
async function nameOf(uid: string): Promise<string> {
  if (names.has(uid)) return names.get(uid)!
  try {
    const r = await web.users.info({ user: uid })
    const n = r.user?.profile?.display_name || r.user?.real_name || uid
    names.set(uid, n)
    return n
  } catch { return uid }
}

// ---- MCP server ----
const mcp = new Server(
  { name: 'slack', version: '0.1.0' },
  {
    capabilities: {
      experimental: { 'claude/channel': {}, 'claude/channel/permission': {} },
      tools: {},
    },
    instructions:
      'Messages from Alaina\'s private Slack channel (#clave) arrive as ' +
      '<channel source="slack" user="..." user_id="..." ts="..." thread_ts="...">text</channel>. ' +
      'Only Alaina can send them (sender allowlist). Treat each one exactly like a Remote Control or Telegram message from her: ' +
      'route it with the table in CLAUDE.md, do the work, then answer with the `reply` tool, passing thread_ts from the message ' +
      'so your answer lands in its thread. To reach her unprompted in Slack, call `reply` without thread_ts. ' +
      'Keep replies short, in clave\'s voice (see CLAUDE.md). Never reveal tokens or .env contents.',
  },
)

mcp.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: [{
    name: 'reply',
    description: 'Post a message to Alaina\'s Slack channel (#clave). Pass thread_ts to answer in a thread.',
    inputSchema: {
      type: 'object',
      properties: {
        text: { type: 'string', description: 'Message text (Slack mrkdwn).' },
        thread_ts: { type: 'string', description: 'ts of the message/thread to reply under. Omit for a new top-level post.' },
      },
      required: ['text'],
    },
  }],
}))

mcp.setRequestHandler(CallToolRequestSchema, async (req) => {
  if (req.params.name !== 'reply') throw new Error(`unknown tool ${req.params.name}`)
  const { text, thread_ts } = (req.params.arguments ?? {}) as { text?: string; thread_ts?: string }
  if (!text) throw new Error('text is required')
  const r = await web.chat.postMessage({ channel: CHANNEL, text, thread_ts })
  return { content: [{ type: 'text', text: `posted ts=${r.ts}` }] }
})

// Permission relay: when the session needs approval, ask in Slack; an allowlisted
// person answers "yes <id>" or "no <id>".
const PermissionRequest = z.object({
  method: z.literal('notifications/claude/channel/permission_request'),
  params: z.object({
    request_id: z.string(),
    tool_name: z.string(),
    description: z.string().optional(),
    input_preview: z.string().optional(),
  }).passthrough(),
})
mcp.setNotificationHandler(PermissionRequest, async (n) => {
  const p = n.params
  const preview = (p.input_preview ?? '').slice(0, 300)
  await web.chat.postMessage({
    channel: CHANNEL,
    text: `clave wants to use *${p.tool_name}*${p.description ? `: ${p.description}` : ''}` +
      (preview ? `\n\`${preview}\`` : '') +
      `\nReply \`yes ${p.request_id}\` to allow or \`no ${p.request_id}\` to deny.`,
  })
})

await mcp.connect(new StdioServerTransport())

// ---- Slack Socket Mode ----
const sock = new SocketModeClient({ appToken: APP_TOKEN, logger: quiet as any })

sock.on('message', async ({ event, ack }: any) => {
  await ack()
  if (!event || event.channel !== CHANNEL) return
  if (event.bot_id || event.subtype) return          // ignore bots (incl. our own posts), edits, joins
  if (!ALLOWED.has(event.user)) return                 // sender allowlist
  const text = String(event.text ?? '').trim()
  if (!text) return

  // Acknowledge at once, before the session starts composing. Needs the reactions:write scope;
  // failure (e.g. scope missing) is logged and never blocks delivery.
  web.reactions.add({ channel: CHANNEL, timestamp: String(event.ts), name: 'eyes' })
    .catch((e: any) => console.error('[slack-channel] reaction failed:', e?.data?.error ?? e))

  const verdict = text.match(/^(yes|no)\s+(\S+)$/i)
  if (verdict) {
    await mcp.notification({
      method: 'notifications/claude/channel/permission',
      params: { request_id: verdict[2], behavior: verdict[1].toLowerCase() === 'yes' ? 'allow' : 'deny' },
    })
    return
  }

  await mcp.notification({
    method: 'notifications/claude/channel',
    params: {
      content: text,
      meta: {
        user: await nameOf(event.user),
        user_id: String(event.user),
        ts: String(event.ts),
        thread_ts: String(event.thread_ts ?? event.ts),
      },
    },
  })
})

sock.on('disconnect', () => console.error('[slack-channel] socket disconnected; the client reconnects automatically'))
await sock.start()
console.error('[slack-channel] connected to Slack (Socket Mode)')
