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
}

export interface MessageDetail extends MessageSummary {
  body: string | null
  sender_id: string | null
  headers: Record<string, string> | null
  edits: { at: string; previous: string | null }[]
  attachment_list: { filename: string | null; mime_type: string | null; size: number | null }[]
}

export interface Health {
  ingesters: { key: string; updated_at: string }[]
  message_counts: Record<string, number>
}

export class Unauthorized extends Error {}

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    ...init,
  })
  if (res.status === 401) throw new Unauthorized()
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`)
  return res.json() as Promise<T>
}

export const api = {
  me: () => call<{ user: string }>('/api/me'),
  login: (password: string) =>
    call('/api/login', { method: 'POST', body: JSON.stringify({ password }) }),
  logout: () => call('/api/logout', { method: 'POST' }),
  health: () => call<Health>('/api/health'),
  message: (id: number) => call<MessageDetail>(`/api/messages/${id}`),
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
