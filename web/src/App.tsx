import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import {
  Alert,
  Button,
  Card,
  Chip,
  Disclosure,
  Header,
  Input,
  Label,
  ListBox,
  SearchField,
  Spinner,
  TextField,
} from '@heroui/react'
import { api, Unauthorized } from './api'
import type { Alert as AlertItem, Health, MessageDetail, MessageSummary, Rule, RuleDraft, Source, TriageInfo } from './api'
import { EMPTY_DRAFT, RuleEditor, RulesPage, VISIBILITY_TEXT, draftFromMessage } from './Rules'

const ALL = 'all'
const SOURCES: { id: string; label: string }[] = [
  { id: ALL, label: 'Everything' },
  { id: 'whatsapp', label: 'WhatsApp' },
  { id: 'discord', label: 'Discord' },
  { id: 'email', label: 'Mail' },
]
const SOURCE_LABEL: Record<string, string> = { whatsapp: 'WhatsApp', discord: 'Discord', email: 'Mail' }
const POLL_MS = 20_000
const STALE_MS = 12 * 60_000

export default function App() {
  const [authed, setAuthed] = useState<boolean | null>(null)

  useEffect(() => {
    api.me().then(() => setAuthed(true)).catch(() => setAuthed(false))
  }, [])

  if (authed === null) return null
  if (!authed) return <Login onDone={() => setAuthed(true)} />
  return <Inbox onSignedOut={() => setAuthed(false)} />
}

function Login({ onDone }: { onDone: () => void }) {
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function submit(e: FormEvent) {
    e.preventDefault()
    setBusy(true)
    setError(null)
    try {
      await api.login(password)
      onDone()
    } catch (err) {
      setError(err instanceof Unauthorized
        ? 'That password is not correct.'
        : 'Majordome is not reachable. Check that the server is running.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <main className="grid min-h-full place-items-center bg-[var(--livery)] p-4">
      <div className="w-full max-w-sm">
        <h1 className="wordmark mb-8 text-5xl text-[var(--livery-ink)]">Majordome</h1>
        <Card className="p-2">
          <form onSubmit={submit} className="grid gap-4">
            <TextField type="password" value={password} onChange={setPassword} autoFocus
              autoComplete="current-password" isInvalid={!!error}>
              <Label>Password</Label>
              <Input />
            </TextField>
            {error && <Alert status="danger"><Alert.Content><Alert.Description>{error}</Alert.Description></Alert.Content></Alert>}
            <Button type="submit" variant="primary" isDisabled={busy || !password} fullWidth>
              {busy ? <Spinner size="sm" color="current" /> : null}
              {busy ? 'Signing in…' : 'Sign in'}
            </Button>
          </form>
        </Card>
      </div>
    </main>
  )
}

function Inbox({ onSignedOut }: { onSignedOut: () => void }) {
  const [source, setSource] = useState<string>(ALL)
  const [query, setQuery] = useState('')
  const [appliedQuery, setAppliedQuery] = useState('')
  const [messages, setMessages] = useState<MessageSummary[]>([])
  const [loading, setLoading] = useState(true)
  const [exhausted, setExhausted] = useState(false)
  const [selected, setSelected] = useState<number | null>(() => {
    const m = Number(new URLSearchParams(window.location.search).get('m'))
    return Number.isInteger(m) && m > 0 ? m : null
  })
  const [health, setHealth] = useState<Health | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [view, setView] = useState<'inbox' | 'rules' | 'alerts'>('inbox')
  const [editor, setEditor] = useState<{ rule: Rule | null; initial: RuleDraft } | null>(null)
  const [rulesVersion, setRulesVersion] = useState(0)
  const [notice, setNotice] = useState<string | null>(null)
  const sourceFilter: Source | null = source === ALL ? null : source

  const handle = useCallback((err: unknown) => {
    if (err instanceof Unauthorized) onSignedOut()
    else setError('Could not load messages. Majordome will retry automatically.')
  }, [onSignedOut])

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const { messages } = await api.messages({ q: appliedQuery, source: sourceFilter })
      setMessages(messages)
      setExhausted(messages.length < 60)
      setError(null)
    } catch (err) { handle(err) } finally { setLoading(false) }
  }, [appliedQuery, sourceFilter, handle])

  useEffect(() => { load() }, [load])

  // Poll for new messages and ingester health.
  useEffect(() => {
    const tick = async () => {
      api.health().then(setHealth).catch(() => {})
      if (appliedQuery) return
      try {
        const { messages: fresh } = await api.messages({ source: sourceFilter })
        setMessages((prev) => {
          const known = new Set(prev.map((m) => m.id))
          const added = fresh.filter((m) => !known.has(m.id))
          return added.length ? [...added, ...prev] : prev
        })
      } catch (err) { handle(err) }
    }
    tick()
    const t = setInterval(tick, POLL_MS)
    return () => clearInterval(t)
  }, [sourceFilter, appliedQuery, handle])

  async function loadOlder() {
    const last = messages[messages.length - 1]
    if (!last) return
    try {
      const { messages: older } = await api.messages({ q: appliedQuery, source: sourceFilter, before: last })
      setMessages((prev) => [...prev, ...older])
      setExhausted(older.length < 60)
    } catch (err) { handle(err) }
  }

  const days = useMemo(() => groupByDay(messages), [messages])
  const reading = selected !== null && view === 'inbox'
  const openEditor = useCallback((rule: Rule | null, initial?: RuleDraft) =>
    setEditor({ rule, initial: rule ?? initial ?? EMPTY_DRAFT }), [])

  return (
    <div className="flex h-full flex-col md:grid md:grid-cols-[200px_1fr] lg:grid-cols-[220px_minmax(320px,440px)_1fr]">
      {/* Rail */}
      <aside className={`rail flex flex-wrap items-center gap-3 bg-[var(--livery)] px-4 py-3 text-[var(--livery-ink)]
        md:flex-col md:flex-nowrap md:items-stretch md:gap-7 md:overflow-y-auto md:px-3.5 md:py-6 ${reading ? 'max-md:hidden' : ''}`}>
        <h1 className="wordmark px-2.5 text-2xl md:text-[1.75rem]">Majordome</h1>
        <ListBox aria-label="Sources" selectionMode="single"
          selectedKeys={view === 'inbox' ? [source] : []}
          onSelectionChange={(keys) => { const k = [...keys][0]; if (k) { setSource(String(k)); setSelected(null); setView('inbox') } }}
          className="flex gap-1 overflow-x-auto bg-transparent p-0 md:grid md:gap-0.5">
          {SOURCES.map((s) => (
            <ListBox.Item key={s.id} id={s.id} textValue={s.label}
              className="flex justify-between gap-4 whitespace-nowrap rounded-md px-2.5 py-1.5">
              <span>{s.label}</span>
              <span className="text-xs tabular-nums max-md:hidden">{countFor(health, s.id)}</span>
            </ListBox.Item>
          ))}
        </ListBox>
        <nav aria-label="Majordome" className="flex gap-1 md:grid md:gap-0.5">
          {([['alerts', 'Alerts'], ['rules', 'Rules']] as const).map(([id, label]) => (
            <button key={id} type="button" aria-current={view === id ? 'page' : undefined}
              onClick={() => setView((v) => (v === id ? 'inbox' : id))}
              className={`flex justify-between gap-4 rounded-md px-2.5 py-1.5 text-left hover:text-[var(--livery-ink)] ${view === id
                ? 'bg-white/10 text-[var(--livery-ink)] shadow-[inset_3px_0_0_var(--accent)]'
                : 'text-[var(--livery-muted)]'}`}>
              <span>{label}</span>
              {id === 'alerts' && !!health?.unread_alerts && (
                <span className="rounded-full bg-[var(--accent)] px-1.5 text-xs font-bold text-[var(--accent-foreground)]">
                  {health.unread_alerts}<span className="sr-only"> unread</span>
                </span>
              )}
            </button>
          ))}
        </nav>
        <Status health={health} />
        <Button variant="ghost" size="sm" onPress={() => { api.logout().finally(onSignedOut) }}
          className="mt-auto self-start text-[var(--livery-muted)] max-md:hidden">
          Sign out
        </Button>
      </aside>

      {/* Timeline */}
      <section aria-label="Messages"
        className={`timeline flex-1 overflow-y-auto border-[var(--border)] pb-6 md:border-r ${reading ? 'max-lg:hidden' : ''} ${view !== 'inbox' ? 'hidden' : ''}`}>
        <form role="search" className="sticky top-0 z-10 bg-[var(--background)] p-4"
          onSubmit={(e) => { e.preventDefault(); setAppliedQuery(query.trim()) }}>
          <SearchField aria-label="Search messages" value={query} fullWidth
            onChange={(v) => { setQuery(v); if (!v) setAppliedQuery('') }}
            onSubmit={(v) => setAppliedQuery(v.trim())}>
            <SearchField.Group>
              <SearchField.SearchIcon />
              <SearchField.Input placeholder="Search every app at once" />
              <SearchField.ClearButton />
            </SearchField.Group>
          </SearchField>
        </form>

        {error && (
          <Alert status="danger" className="mx-4 mb-2">
            <Alert.Content><Alert.Description>{error}</Alert.Description></Alert.Content>
          </Alert>
        )}
        {loading && messages.length === 0 && <div className="grid place-items-center py-10"><Spinner /></div>}
        {!loading && messages.length === 0 && (
          <p className="mx-4 my-8 max-w-[36ch] text-[var(--muted)]">
            {appliedQuery
              ? <>No messages match “{appliedQuery}”.</>
              : <>Nothing here yet. New messages appear as they arrive; history starts from the moment each account was connected.</>}
          </p>
        )}

        {messages.length > 0 && (
          <ListBox aria-label="Messages" selectionMode="single"
            selectedKeys={selected !== null ? [selected] : []}
            onSelectionChange={(keys) => { const k = [...keys][0]; setSelected(k !== undefined ? Number(k) : null) }}
            className="bg-transparent p-0">
            {days.map(([day, items]) => (
              <ListBox.Section key={day}>
                <Header className="font-display mx-4 mb-1 mt-3.5 text-[0.95rem] font-semibold tracking-tight text-[var(--muted)]">
                  {day}
                </Header>
                {items.map((m) => <MessageRow key={m.id} m={m} />)}
              </ListBox.Section>
            ))}
          </ListBox>
        )}
        {messages.length > 0 && !exhausted && (
          <div className="mt-4 flex justify-center">
            <Button variant="outline" size="sm" onPress={loadOlder}>Show older messages</Button>
          </div>
        )}
      </section>

      {view === 'inbox' && (
        <Reader id={selected} onBack={() => setSelected(null)} onError={handle} version={rulesVersion}
          onMakeRule={(m) => openEditor(null, draftFromMessage(m))}
          className={reading ? 'flex-1' : 'max-lg:hidden'} />
      )}
      {view === 'alerts' && (
        <div className="flex flex-1 lg:col-span-2">
          <AlertsPage onError={handle} version={health?.unread_alerts ?? 0}
            onOpen={(id) => { setSelected(id); setView('inbox') }}
            onChanged={() => api.health().then(setHealth).catch(() => {})} />
        </div>
      )}
      {view === 'rules' && (
        <div className="flex flex-1 lg:col-span-2">
          <RulesPage onError={handle} onEdit={(r) => openEditor(r)} version={rulesVersion}
            onNotice={(msg) => { setNotice(msg); load() }} />
        </div>
      )}

      {editor && (
        <RuleEditor rule={editor.rule} initial={editor.initial} onError={handle}
          onClose={() => setEditor(null)}
          onSaved={(msg) => { setEditor(null); setNotice(msg); setRulesVersion((v) => v + 1); load() }} />
      )}
      {notice && (
        <div role="status" className="fixed bottom-4 left-1/2 z-50 w-[min(560px,calc(100%-32px))] -translate-x-1/2">
          <Alert status="success" className="border border-[var(--border)] bg-[var(--surface-secondary)] shadow-lg">
            <Alert.Content><Alert.Description>{notice}</Alert.Description></Alert.Content>
            <Button size="sm" variant="ghost" onPress={() => setNotice(null)}>Dismiss</Button>
          </Alert>
        </div>
      )}
    </div>
  )
}

function SourceChip({ source, account }: { source: string; account: string }) {
  return (
    <Chip size="sm" className={`tag-${source} shrink-0 rounded px-1.5 text-xs text-[var(--foreground)]`}>
      {source === 'email' ? account : SOURCE_LABEL[source] ?? source}
    </Chip>
  )
}

function MessageRow({ m }: { m: MessageSummary }) {
  const who = m.direction === 'out' ? 'You' : m.sender ?? 'Unknown sender'
  const text = m.snippet || (m.attachments ? `${m.attachments} attachment${m.attachments > 1 ? 's' : ''}` : '')
  return (
    <ListBox.Item id={m.id} textValue={`${who} ${m.subject ?? ''} ${text}`}
      className="rounded-none px-4 py-2.5">
      <div className="grid w-full min-w-0 gap-0.5">
      <span className="flex justify-between gap-3">
        <span className="truncate font-bold">{who}</span>
        <time dateTime={m.sent_at} className="shrink-0 text-sm tabular-nums text-[var(--muted)]">{formatTime(m.sent_at)}</time>
      </span>
      <span className="flex min-w-0 items-center gap-2 text-xs text-[var(--muted)]">
        <SourceChip source={m.source} account={m.account} />
        {m.conversation_kind !== 'dm' && m.conversation && <span className="truncate">{m.conversation}</span>}
        <TriageMark t={m.triage} />
        {m.visibility && m.visibility !== 'always' && (
          <span className="ml-auto shrink-0">{VISIBILITY_TEXT[m.visibility].short}</span>
        )}
      </span>
      {m.subject && <span className="truncate text-[0.95rem]">{m.subject}</span>}
      <span className={`line-clamp-2 text-sm text-[var(--muted)] [overflow-wrap:anywhere] ${m.deleted ? 'line-through' : ''}`}>
        {text}
      </span>
      </div>
    </ListBox.Item>
  )
}

function Reader({ id, onBack, onError, onMakeRule, className, version }: {
  id: number | null; onBack: () => void; onError: (e: unknown) => void
  onMakeRule: (m: MessageDetail) => void; className: string; version: number
}) {
  const [m, setM] = useState<MessageDetail | null>(null)
  const ref = useRef<HTMLElement>(null)

  useEffect(() => {
    setM(null)
    if (id === null) return
    api.message(id).then((d) => { setM(d); ref.current?.focus() }).catch(onError)
  }, [id, onError, version])

  if (id === null) {
    return (
      <article className={`grid place-items-center bg-[var(--surface)] text-[var(--muted)] ${className}`}>
        <p>Select a message to read it.</p>
      </article>
    )
  }
  return (
    <article ref={ref} tabIndex={-1} aria-live="polite"
      className={`overflow-y-auto bg-[var(--surface)] px-4 py-4 outline-none md:px-[clamp(16px,4vw,56px)] md:py-8 ${className}`}>
      <Button variant="ghost" size="sm" onPress={onBack} className="mb-4 lg:hidden">Back to messages</Button>
      {!m && <Spinner />}
      {m && (
        <div className="max-w-[72ch]">
          <header className="mb-6">
            <p className="mb-3 flex items-center gap-2.5 text-sm text-[var(--muted)]">
              <SourceChip source={m.source} account={m.account} />
              {m.conversation && <span>{m.conversation}</span>}
            </p>
            {m.subject && (
              <h2 className="font-display mb-3 text-[clamp(1.4rem,2.4vw,1.9rem)] font-bold leading-tight tracking-tight">
                {m.subject}
              </h2>
            )}
            <p className="flex flex-wrap items-baseline gap-2">
              <strong>{m.direction === 'out' ? 'You' : m.sender ?? 'Unknown sender'}</strong>
              {m.source === 'email' && m.sender_id && m.sender_id !== m.sender && (
                <span className="text-sm text-[var(--muted)] [overflow-wrap:anywhere]">{m.sender_id}</span>
              )}
            </p>
            <p className="mt-0.5 text-sm text-[var(--muted)]">
              <time dateTime={m.sent_at}>{new Date(m.sent_at).toLocaleString()}</time>
              {m.edited && ' (edited)'}{m.deleted && ' (deleted by sender)'}
            </p>
          </header>

          <div className="whitespace-pre-wrap leading-relaxed [overflow-wrap:anywhere]">
            {m.metadata_only
              ? <em className="text-[var(--muted)]">A rule keeps only who sent this and when, not its content.</em>
              : m.body ?? <em>No text content.</em>}
          </div>

          {m.attachment_list.length > 0 && (
            <ul aria-label="Attachments" className="mt-6 grid gap-1.5">
              {m.attachment_list.map((a, i) => (
                <li key={i} className="rounded-md border border-[var(--border)] px-3 py-2 [overflow-wrap:anywhere]">
                  {a.filename ?? 'Unnamed file'}{' '}
                  <span className="text-sm text-[var(--muted)]">{a.mime_type}{a.size ? `, ${formatSize(a.size)}` : ''}</span>
                </li>
              ))}
            </ul>
          )}

          {m.triage && <TriagePanel t={m.triage} />}
          <PolicyPanel m={m} onMakeRule={() => onMakeRule(m)} />

          {m.edits.length > 0 && (
            <Section title="Earlier versions">
              {m.edits.map((e, i) => (
                <p key={i} className="my-1.5"><time>{new Date(e.at).toLocaleString()}</time> {e.previous}</p>
              ))}
            </Section>
          )}

          {m.headers && (
            <Section title="Email headers">
              <dl className="grid gap-2 text-sm">
                {Object.entries(m.headers).map(([k, v]) => (
                  <div key={k}><dt className="font-bold">{k}</dt><dd className="text-[var(--muted)] [overflow-wrap:anywhere]">{v}</dd></div>
                ))}
              </dl>
            </Section>
          )}
        </div>
      )}
    </article>
  )
}

const CATEGORY_TEXT: Record<string, string> = {
  personal: 'Personal', work: 'Work', transactional: 'Orders, bills and accounts', newsletter: 'Newsletter or promotion',
  notification: 'Automated notification', spam: 'Spam', scam: 'Scam', other: 'Other',
}
const IMPORTANCE_TEXT: Record<string, string> = {
  interrupt: 'Important: you were notified', digest: 'Worth reading today', silent: 'No need to read',
}

function isScam(t: TriageInfo): boolean {
  return t.category === 'scam' || t.scam_risk >= 0.8
}

function TriageMark({ t }: { t?: TriageInfo | null }) {
  if (!t) return null
  if (isScam(t)) return <span className="shrink-0 font-bold text-[var(--danger)]">Likely scam</span>
  if (t.importance === 'interrupt') return <span className="shrink-0 font-bold text-[var(--accent)]">Important</span>
  return null
}

function TriagePanel({ t }: { t: TriageInfo }) {
  const scam = isScam(t)
  return (
    <section aria-label="Majordome's assessment"
      className={`mt-8 rounded-lg border p-4 ${scam ? 'border-[var(--danger)]' : 'border-[var(--border)]'}`}>
      {scam && (
        <p className="mb-2 font-bold text-[var(--danger)]">
          This looks like a scam. Don't click its links, reply, or share codes or payment details.
        </p>
      )}
      <p>{t.summary}</p>
      <dl className="mt-3 grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
        <dt className="text-[var(--muted)]">Kind</dt><dd>{CATEGORY_TEXT[t.category] ?? t.category}</dd>
        <dt className="text-[var(--muted)]">Priority</dt><dd>{IMPORTANCE_TEXT[t.importance] ?? t.importance}</dd>
        <dt className="text-[var(--muted)]">Scam risk</dt><dd>{Math.round(t.scam_risk * 100)}%</dd>
        <dt className="text-[var(--muted)]">Why</dt><dd>{t.reason}</dd>
      </dl>
    </section>
  )
}

function AlertsPage({ onOpen, onError, onChanged, version }: {
  onOpen: (messageId: number) => void; onError: (e: unknown) => void; onChanged: () => void; version: number
}) {
  const [alerts, setAlerts] = useState<AlertItem[] | null>(null)

  const load = useCallback(() => {
    api.alerts().then((r) => setAlerts(r.notifications)).catch(onError)
  }, [onError])
  useEffect(() => { load() }, [load, version])

  async function markAll() {
    try { await api.markAlertsRead(); load(); onChanged() } catch (e) { onError(e) }
  }

  async function open(a: AlertItem) {
    if (!a.message) return
    if (!a.read) api.markAlertsRead([a.id]).then(onChanged).catch(() => {})
    onOpen(a.message.id)
  }

  return (
    <section aria-labelledby="alerts-title" className="flex-1 overflow-y-auto px-4 py-6 md:px-10 md:py-8">
      <div className="max-w-[72ch]">
        <div className="mb-2 flex flex-wrap items-center justify-between gap-3">
          <h2 id="alerts-title" className="font-display text-3xl font-bold tracking-tight">Alerts</h2>
          {alerts?.some((a) => !a.read) && <Button variant="secondary" size="sm" onPress={markAll}>Mark all as read</Button>}
        </div>
        <p className="mb-6 text-[var(--muted)]">
          Messages Majordome thought you should see right away. Only messages the agent may read are assessed.
        </p>
        {alerts === null && <Spinner />}
        {alerts?.length === 0 && <p className="text-[var(--muted)]">No alerts yet.</p>}
        <ul className="grid gap-2">
          {alerts?.map((a) => (
            <li key={a.id}>
              <button type="button" onClick={() => open(a)} disabled={!a.message}
                className={`w-full rounded-lg border p-3 text-left hover:bg-[var(--surface-secondary)] ${a.kind === 'scam_warning'
                  ? 'border-[var(--danger)]' : 'border-[var(--border)]'} ${a.read ? 'opacity-70' : ''}`}>
                <span className="flex flex-wrap items-baseline justify-between gap-2">
                  <span className="font-bold">
                    {!a.read && <span aria-hidden="true" className="mr-2 inline-block size-2 rounded-full bg-[var(--accent)]" />}
                    {a.kind === 'scam_warning' ? 'Likely scam: ' : ''}{a.message?.sender ?? 'Deleted message'}
                    {!a.read && <span className="sr-only"> (unread)</span>}
                  </span>
                  <time dateTime={a.created_at} className="text-sm text-[var(--muted)]">{new Date(a.created_at).toLocaleString()}</time>
                </span>
                {a.message && (
                  <span className="mt-1 block text-sm text-[var(--muted)]">
                    {SOURCE_LABEL[a.message.source] ?? a.message.source}
                    {a.message.conversation_kind !== 'dm' && a.message.conversation ? `, ${a.message.conversation}` : ''}
                  </span>
                )}
                <span className="mt-1 block">{a.message?.triage?.summary}</span>
                <span className="mt-1 block text-xs text-[var(--muted)]">{deliveryText(a.delivery)}</span>
              </button>
            </li>
          ))}
        </ul>
      </div>
    </section>
  )
}

function deliveryText(d: Record<string, string>): string {
  const discord = d.discord
  if (!discord) return 'Not sent yet'
  if (discord === 'sent') return 'Sent to Discord'
  if (discord === 'not configured') return 'Shown here only: no Discord webhook is set'
  return `Discord delivery failed (${discord.replace(/^error: /, '')})`
}

function PolicyPanel({ m, onMakeRule }: { m: MessageDetail; onMakeRule: () => void }) {
  const rules = m.policy.matched_rules ?? []
  const v = VISIBILITY_TEXT[m.visibility]
  return (
    <section aria-label="How Majordome handles this message"
      className="mt-8 rounded-lg border border-[var(--border)] p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h3 className="font-bold">{v.short}</h3>
          <p className="text-sm text-[var(--muted)]">{v.long}</p>
        </div>
        <Button size="sm" variant="secondary" onPress={onMakeRule}>Make a rule from this message</Button>
      </div>
      <p className="mt-3 text-sm">
        {rules.length
          ? <>Because of {rules.length === 1 ? 'the rule' : 'the rules'} {rules.map((r) => `“${r.name}”`).join(', ')}.</>
          : <>No rule sets this, so your default applies (change it on the Rules page).</>}
        {m.expires_at && <> It will be deleted on {new Date(m.expires_at).toLocaleDateString()}.</>}
      </p>
      {m.visibility !== 'never' && m.agent_view !== null && m.agent_view !== m.body && (
        <Section title="What the agent sees">
          <p className="whitespace-pre-wrap text-sm [overflow-wrap:anywhere]">{m.agent_view}</p>
        </Section>
      )}
    </section>
  )
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <Disclosure className="mt-8">
      <Disclosure.Heading>
        <Disclosure.Trigger className="flex items-center gap-1 text-[var(--accent)]">
          {title}
          <Disclosure.Indicator />
        </Disclosure.Trigger>
      </Disclosure.Heading>
      <Disclosure.Content>
        <Disclosure.Body className="pt-3">{children}</Disclosure.Body>
      </Disclosure.Content>
    </Disclosure>
  )
}

const DOT: Record<string, string> = {
  ok: 'bg-[#4f9a6b]', warn: 'bg-[#d9b25c]', error: 'bg-[#d98a6c]', unknown: 'bg-[var(--livery-muted)]',
}

function Status({ health }: { health: Health | null }) {
  if (!health) return null
  const now = Date.now()
  const lines = [
    ...health.bridges.map((b) => ({
      key: b.name, name: SOURCE_LABEL[b.name] ?? b.name, status: b.status,
      note: b.status === 'ok' ? null : b.label, detail: b.detail,
    })),
    ...health.ingesters.map((i) => {
      const stale = now - new Date(i.updated_at).getTime() > STALE_MS
      return { key: i.key, name: ingesterLabel(i.key), status: stale ? 'error' : 'ok',
        note: stale ? 'Not syncing' : null, detail: null }
    }),
  ]
  return (
    <section aria-label="Connections" className="max-md:hidden">
      <h2 className="mx-2.5 mb-1.5 text-sm text-[var(--livery-muted)]">Connections</h2>
      <ul className="grid gap-1.5 px-2.5">
        {lines.map((l) => (
          <li key={l.key} className="text-sm [overflow-wrap:anywhere]" title={l.detail ?? undefined}>
            <span className="flex items-center gap-2">
              <span aria-hidden="true" className={`size-[7px] shrink-0 rounded-full ${DOT[l.status]}`} />
              <span>{l.name}</span>
              {!l.note && <span className="sr-only">connected</span>}
            </span>
            {l.note && <span className="ml-[15px] block text-xs text-[#f2c3b2]">{l.note}</span>}
          </li>
        ))}
      </ul>
    </section>
  )
}

function ingesterLabel(key: string): string {
  const m = key.match(/^imap\.(.+)\.([^.]+)$/)
  return m ? m[1] : key
}

function countFor(h: Health | null, source: string): string {
  if (!h) return ''
  const n = source !== ALL ? h.message_counts[source] ?? 0 : Object.values(h.message_counts).reduce((a, b) => a + b, 0)
  return n ? n.toLocaleString() : ''
}

function groupByDay(messages: MessageSummary[]): [string, MessageSummary[]][] {
  const groups = new Map<string, MessageSummary[]>()
  for (const m of messages) {
    const label = dayLabel(new Date(m.sent_at))
    groups.set(label, [...(groups.get(label) ?? []), m])
  }
  return [...groups.entries()]
}

function dayLabel(d: Date): string {
  const today = new Date()
  const yesterday = new Date(today)
  yesterday.setDate(today.getDate() - 1)
  if (d.toDateString() === today.toDateString()) return 'Today'
  if (d.toDateString() === yesterday.toDateString()) return 'Yesterday'
  return d.toLocaleDateString(undefined, {
    weekday: 'long', day: 'numeric', month: 'long',
    year: d.getFullYear() === today.getFullYear() ? undefined : 'numeric',
  })
}

function formatTime(iso: string): string {
  return new Date(iso).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
}

function formatSize(n: number): string {
  if (n < 1024) return `${n} B`
  if (n < 1024 ** 2) return `${(n / 1024).toFixed(0)} KB`
  return `${(n / 1024 ** 2).toFixed(1)} MB`
}
