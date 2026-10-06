export type Source = 'whatsapp' | 'discord' | 'email' | string

export interface MessageSummary {
  id: number
  source: Source
  account: string
  conversation_id: number
  conversation: string | null
  conversation_kind: string
  sender: string | null
  direction: 'in' | 'out'
  sent_at: string
  subject: string | null
  snippet: string
  attachments: number
  edited: boolean
  deleted: boolean
  visibility?: Visibility
}

export type Visibility = 'always' | 'on_request' | 'on_request_approval' | 'never'

export interface PolicyExplanation {
  storage?: string
  visibility?: Visibility
  redactors?: string[]
  patterns?: string[]
  retention_days?: number | null
  matched_rules?: { id: number; name: string }[]
}

export interface MessageDetail extends MessageSummary {
  body: string | null
  sender_id: string | null
  headers: Record<string, string> | null
  edits: { at: string; previous: string | null }[]
  attachment_list: { filename: string | null; mime_type: string | null; size: number | null }[]
  visibility: Visibility
  agent_view: string | null
  policy: PolicyExplanation
  expires_at: string | null
  account_id: number
  metadata_only: boolean
}

export interface Matchers {
  sources?: string[]
  accounts?: number[]
  conversations?: number[]
  conversation_kinds?: string[]
  senders?: string[]
  sender_domains?: string[]
  keywords?: string[]
  regex?: string
  has_attachment?: boolean | null
}

export interface Outcomes {
  storage?: 'store' | 'metadata_only' | 'drop'
  visibility?: Visibility
  redact_patterns?: string[]
  retention_days?: number | null
}

export interface RuleDraft {
  name: string
  enabled: boolean
  matchers: Matchers
  outcomes: Outcomes
}

export interface Rule extends RuleDraft {
  id: number
  updated_at: string
}

export interface ApplyStats {
  examined: number
  matched: number
  changed: number
  wiped: number
  deleted: number
  samples: { id: number; source: string; conversation: string | null; sender: string | null; snippet: string; sent_at: string }[] | null
}

export interface Account { id: number; source: string; name: string; external_id: string }

export interface BridgeHealth {
  name: string
  status: 'ok' | 'warn' | 'error' | 'unknown'
  label: string
  detail?: string | null
}

export interface Health {
  ingesters: { key: string; updated_at: string }[]
  bridges: BridgeHealth[]
  message_counts: Record<string, number>
}

export class Unauthorized extends Error {}

/** A request the server refused, with the reasons it gave (e.g. rule validation errors). */
export class Rejected extends Error {
  errors: string[]
  constructor(errors: string[]) {
    super(errors.join(' '))
    this.errors = errors
  }
}

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    ...init,
  })
  if (res.status === 401) throw new Unauthorized()
  if (res.status === 422) {
    const body = await res.json().catch(() => null)
    throw new Rejected(body?.detail?.errors ?? ['The server refused this request.'])
  }
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`)
  return res.json() as Promise<T>
}

const json = (method: string, body: unknown): RequestInit => ({ method, body: JSON.stringify(body) })

export const api = {
  me: () => call<{ user: string }>('/api/me'),
  login: (password: string) =>
    call('/api/login', { method: 'POST', body: JSON.stringify({ password }) }),
  logout: () => call('/api/logout', { method: 'POST' }),
  health: () => call<Health>('/api/health'),
  message: (id: number) => call<MessageDetail>(`/api/messages/${id}`),
  rules: () => call<{ rules: Rule[]; redactors: { id: string; label: string }[] }>('/api/rules'),
  previewRule: (draft: RuleDraft, id?: number) =>
    call<ApplyStats>(`/api/rules/preview${id ? `?rule_id=${id}` : ''}`, json('POST', draft)),
  createRule: (draft: RuleDraft) => call<{ rule: Rule; applied: ApplyStats }>('/api/rules', json('POST', draft)),
  updateRule: (id: number, draft: RuleDraft) =>
    call<{ rule: Rule; applied: ApplyStats }>(`/api/rules/${id}`, json('PUT', draft)),
  deleteRule: (id: number) => call<{ applied: ApplyStats }>(`/api/rules/${id}`, { method: 'DELETE' }),
  accounts: () => call<{ accounts: Account[] }>('/api/accounts'),
  defaults: () => call<{ visibility: Visibility }>('/api/policy/defaults'),
  setDefaults: (visibility: Visibility) =>
    call<{ visibility: Visibility; applied: ApplyStats }>('/api/policy/defaults', json('PUT', { visibility })),
  lookup: (conversationIds: number[], senders: string[]) => {
    const p = new URLSearchParams()
    conversationIds.forEach((c) => p.append('conversation_ids', String(c)))
    senders.forEach((x) => p.append('senders', x))
    return call<{ conversations: Record<string, string | null>; senders: Record<string, string | null> }>(`/api/lookup?${p}`)
  },
  messages: (opts: { q?: string; source?: Source | null; before?: MessageSummary }) => {
    const p = new URLSearchParams({ limit: '60' })
    if (opts.q) p.set('q', opts.q)
    if (opts.source) p.set('source', opts.source)
    if (opts.before) {
      p.set('before', opts.before.sent_at)
      p.set('before_id', String(opts.before.id))
    }
    return call<{ messages: MessageSummary[] }>(`/api/messages?${p}`)
  },
}
