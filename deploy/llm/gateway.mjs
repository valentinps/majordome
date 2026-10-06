// Majordome LLM gateway: exposes Claude Code (headless, subscription auth) as a narrow
// "system prompt + input + JSON schema -> JSON" HTTP endpoint for the worker.
//
// Runs inside the sandboxed `llm` container (no direct internet, egress proxy to
// api.anthropic.com only). Every call disables all tools and MCP servers, and the gateway
// FAILS CLOSED: if Claude Code reports any tool other than StructuredOutput, or any MCP
// server, the answer is discarded. No dependencies beyond Node's standard library.

import { spawn } from 'node:child_process'
import { createServer } from 'node:http'
import { timingSafeEqual } from 'node:crypto'

const PORT = Number(process.env.GATEWAY_PORT || 8100)
const TOKEN = process.env.GATEWAY_TOKEN || ''
const MAX_CONCURRENT = Number(process.env.GATEWAY_CONCURRENCY || 2)
const TIMEOUT_MS = Number(process.env.GATEWAY_TIMEOUT_MS || 180_000)
const MAX_BODY = 512 * 1024
const MODELS = new Set(['haiku', 'sonnet', 'opus'])
const ALLOWED_TOOLS = new Set(['StructuredOutput'])

if (TOKEN.length < 32) {
  console.error('GATEWAY_TOKEN must be set (32+ chars)')
  process.exit(1)
}

let running = 0

function authorized(req) {
  const got = Buffer.from((req.headers.authorization || '').replace(/^Bearer /, ''))
  const want = Buffer.from(TOKEN)
  return got.length === want.length && timingSafeEqual(got, want)
}

function send(res, status, body) {
  res.writeHead(status, { 'Content-Type': 'application/json' })
  res.end(JSON.stringify(body))
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    let size = 0
    const chunks = []
    req.on('data', (c) => {
      size += c.length
      if (size > MAX_BODY) { reject(new Error('body too large')); req.destroy() }
      else chunks.push(c)
    })
    req.on('end', () => resolve(Buffer.concat(chunks).toString('utf8')))
    req.on('error', reject)
  })
}

function runClaude({ system, input, schema, model }) {
  const args = [
    '-p',
    '--tools', '',
    '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}',
    '--setting-sources', '',
    '--no-session-persistence',
    '--output-format', 'stream-json', '--verbose',
    '--model', model,
    '--system-prompt', system,
    '--json-schema', JSON.stringify(schema),
  ]
  return new Promise((resolve) => {
    const started = Date.now()
    const child = spawn('claude', args, { stdio: ['pipe', 'pipe', 'pipe'] })
    let out = ''
    let err = ''
    const timer = setTimeout(() => child.kill('SIGKILL'), TIMEOUT_MS)
    child.stdout.on('data', (d) => { out += d })
    child.stderr.on('data', (d) => { err += d })
    child.on('close', (code) => {
      clearTimeout(timer)
      resolve({ code, out, err: err.slice(-2000), ms: Date.now() - started })
    })
    child.stdin.end(input)
  })
}

function interpret(run) {
  let init = null
  let result = null
  const toolCalls = []
  for (const line of run.out.split('\n')) {
    if (!line.trim()) continue
    let ev
    try { ev = JSON.parse(line) } catch { continue }
    if (ev.type === 'system' && ev.subtype === 'init') init = ev
    if (ev.type === 'assistant') {
      for (const c of ev.message?.content || []) if (c.type === 'tool_use') toolCalls.push(c.name)
    }
    if (ev.type === 'result') result = ev
  }
  if (!init) return { error: 'no init event', detail: run.err }
  const tools = (init.tools || []).filter((t) => !ALLOWED_TOOLS.has(t))
  if (tools.length || (init.mcp_servers || []).length) {
    return { error: `refused: tools or MCP available (${[...tools, ...(init.mcp_servers || []).map((m) => m.name)].join(', ')})` }
  }
  if (toolCalls.some((t) => !ALLOWED_TOOLS.has(t))) return { error: 'refused: model attempted a tool call' }
  if (!result) return { error: `no result (exit ${run.code})`, detail: run.err }
  if (result.is_error || result.structured_output == null) {
    return { error: result.result || 'no structured output', detail: result.api_error_status }
  }
  return {
    output: result.structured_output,
    model: init.model,
    usage: result.usage || null,
    duration_ms: run.ms,
  }
}

createServer(async (req, res) => {
  if (req.method === 'GET' && req.url === '/health') return send(res, 200, { ok: true, running })
  if (req.method !== 'POST' || req.url !== '/complete') return send(res, 404, { error: 'not found' })
  if (!authorized(req)) return send(res, 403, { error: 'forbidden' })
  if (running >= MAX_CONCURRENT) return send(res, 429, { error: 'busy' })

  let body
  try { body = JSON.parse(await readBody(req)) } catch { return send(res, 400, { error: 'invalid JSON' }) }
  const { system, input, schema } = body
  const model = body.model || 'haiku'
  if (typeof system !== 'string' || typeof input !== 'string' || typeof schema !== 'object' || !MODELS.has(model)) {
    return send(res, 400, { error: 'expected {system, input, schema, model?}' })
  }

  running += 1
  try {
    const verdict = interpret(await runClaude({ system, input, schema, model }))
    send(res, verdict.error ? 502 : 200, verdict)
  } finally {
    running -= 1
  }
}).listen(PORT, '0.0.0.0', () => console.log(`llm gateway on :${PORT}`))
