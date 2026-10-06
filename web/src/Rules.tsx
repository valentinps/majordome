import { useCallback, useEffect, useState } from 'react'
import type { ReactNode } from 'react'
import {
  Alert,
  Button,
  Card,
  Checkbox,
  CheckboxGroup,
  Chip,
  Description,
  Input,
  Label,
  Modal,
  Radio,
  RadioGroup,
  Spinner,
  Switch,
  TextField,
} from '@heroui/react'
import { api, Rejected } from './api'
import type { Account, ApplyStats, Matchers, Outcomes, Rule, RuleDraft, Visibility } from './api'

export const SOURCE_NAMES: Record<string, string> = { whatsapp: 'WhatsApp', discord: 'Discord', email: 'Mail' }
const KIND_NAMES: Record<string, string> = { dm: 'Direct messages', group: 'Groups', email_thread: 'Email threads' }

export const VISIBILITY_TEXT: Record<Visibility, { short: string; long: string }> = {
  always: { short: 'Agent can read', long: 'The agent can read it and act on it, for example to notify you.' },
  on_request: { short: 'Only when you ask', long: 'The agent ignores it, except when you ask it a question.' },
  on_request_approval: {
    short: 'Only with your approval',
    long: 'The agent must ask your permission each time it wants to read it.',
  },
  never: { short: 'Agent never reads', long: 'The agent never sees this message.' },
}

const STORAGE_TEXT: Record<string, string> = {
  store: 'Keep the full message',
  metadata_only: 'Keep only who sent it, when, and from which app',
  drop: "Don't keep it at all",
}

const RETENTION: { days: number | null; label: string }[] = [
  { days: null, label: 'Forever' },
  { days: 7, label: '1 week' },
  { days: 30, label: '1 month' },
  { days: 90, label: '3 months' },
  { days: 365, label: '1 year' },
]

export const EMPTY_DRAFT: RuleDraft = { name: '', enabled: true, matchers: {}, outcomes: {} }

/* ------------------------------------------------------------------------------------------ */
/* Rules page                                                                                  */
/* ------------------------------------------------------------------------------------------ */

export function RulesPage({ onError, onEdit, version }: {
  onError: (e: unknown) => void
  onEdit: (rule: Rule | null) => void
  version: number
}) {
  const [rules, setRules] = useState<Rule[] | null>(null)
  const [names, setNames] = useState<Names>(EMPTY_NAMES)

  const load = useCallback(async () => {
    try {
      const { rules } = await api.rules()
      setRules(rules)
      setNames(await lookupNames(rules.map((r) => r.matchers)))
    } catch (e) { onError(e) }
  }, [onError])

  useEffect(() => { load() }, [load, version])

  async function toggle(rule: Rule, enabled: boolean) {
    try {
      await api.updateRule(rule.id, { ...rule, enabled })
      load()
    } catch (e) { onError(e) }
  }

  return (
    <section aria-labelledby="rules-title" className="flex-1 overflow-y-auto px-4 py-6 md:px-10 md:py-8">
      <div className="max-w-[72ch]">
        <div className="mb-2 flex flex-wrap items-center justify-between gap-3">
          <h2 id="rules-title" className="font-display text-3xl font-bold tracking-tight">Rules</h2>
          <Button variant="primary" onPress={() => onEdit(null)}>New rule</Button>
        </div>
        <p className="mb-6 text-[var(--muted)]">
          Rules decide what Majordome keeps, what its agent may read, and for how long. When several
          rules match a message, the strictest choice wins, so a new rule can only make things more private.
          One-time codes, card numbers, IBANs, passwords and login links are always hidden from the agent.
        </p>

        {rules === null && <Spinner />}
        {rules?.length === 0 && (
          <Card className="border border-[var(--border)] bg-[var(--surface)] p-2">
            <Card.Content>
              <p>No rules yet: every message is kept, and the agent can read it with sensitive details hidden.</p>
              <p className="mt-2 text-sm text-[var(--muted)]">
                Tip: open a message and use “Make a rule from this message” to start from its sender or conversation.
              </p>
            </Card.Content>
          </Card>
        )}
        <ul className="grid gap-3">
          {rules?.map((r) => (
            <li key={r.id}>
              <Card className={`border border-[var(--border)] bg-[var(--surface)] p-1 ${r.enabled ? '' : 'opacity-60'}`}>
                <div className="flex flex-wrap items-start justify-between gap-4 p-3">
                  <div className="min-w-0 flex-1 basis-64">
                    <h3 className="font-bold">{r.name}</h3>
                    <p className="mt-1 text-sm text-[var(--muted)]">{describe(r, names)}</p>
                    {!r.enabled && <p className="mt-1 text-sm">Paused: this rule has no effect.</p>}
                  </div>
                  <div className="flex shrink-0 items-center gap-3">
                    <Switch isSelected={r.enabled} onChange={(v) => toggle(r, v)} aria-label={`Rule ${r.name} enabled`}>
                      <Switch.Control><Switch.Thumb /></Switch.Control>
                    </Switch>
                    <Button size="sm" variant="secondary" onPress={() => onEdit(r)}>Edit</Button>
                  </div>
                </div>
              </Card>
            </li>
          ))}
        </ul>
      </div>
    </section>
  )
}

/* ------------------------------------------------------------------------------------------ */
/* Rule editor (modal)                                                                         */
/* ------------------------------------------------------------------------------------------ */

export function RuleEditor({ rule, initial, onClose, onSaved, onError }: {
  rule: Rule | null
  initial: RuleDraft
  onClose: () => void
  onSaved: (message: string) => void
  onError: (e: unknown) => void
}) {
  const [draft, setDraft] = useState<RuleDraft>(initial)
  const [accounts, setAccounts] = useState<Account[]>([])
  const [names, setNames] = useState<Names>(EMPTY_NAMES)
  const [preview, setPreview] = useState<ApplyStats | null>(null)
  const [problems, setProblems] = useState<string[]>([])
  const [checking, setChecking] = useState(false)
  const [confirmed, setConfirmed] = useState(false)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    api.accounts().then((r) => setAccounts(r.accounts)).catch(onError)
    lookupNames([initial.matchers]).then(setNames).catch(() => {})
  }, [initial, onError])

  // Live dry-run preview against the stored messages.
  useEffect(() => {
    setConfirmed(false)
    const t = setTimeout(async () => {
      setChecking(true)
      try {
        setPreview(await api.previewRule({ ...draft, name: draft.name || 'Untitled' }, rule?.id))
        setProblems([])
      } catch (e) {
        setPreview(null)
        if (e instanceof Rejected) setProblems(e.errors)
        else onError(e)
      } finally { setChecking(false) }
    }, 450)
    return () => clearTimeout(t)
  }, [draft, rule, onError])

  const setM = (patch: Partial<Matchers>) => setDraft((d) => ({ ...d, matchers: clean({ ...d.matchers, ...patch }) }))
  const setO = (patch: Partial<Outcomes>) => setDraft((d) => ({ ...d, outcomes: clean({ ...d.outcomes, ...patch }) }))
  const destructive = !!preview && (preview.deleted > 0 || preview.wiped > 0)
  const canSave = !busy && !checking && problems.length === 0 && !!draft.name.trim() && (!destructive || confirmed)

  async function save() {
    setBusy(true)
    try {
      const res = rule ? await api.updateRule(rule.id, draft) : await api.createRule(draft)
      onSaved(`${rule ? 'Updated' : 'Created'} “${res.rule.name}”. ${summarizeApplied(res.applied)}`)
    } catch (e) {
      if (e instanceof Rejected) setProblems(e.errors)
      else onError(e)
    } finally { setBusy(false) }
  }

  async function remove() {
    if (!rule) return
    setBusy(true)
    try {
      const res = await api.deleteRule(rule.id)
      onSaved(`Deleted “${rule.name}”. ${summarizeApplied(res.applied)}`)
    } catch (e) { onError(e) } finally { setBusy(false) }
  }

  const sourceAccounts = accounts.filter((a) => !draft.matchers.sources?.length || draft.matchers.sources.includes(a.source))

  return (
    <Modal.Backdrop isOpen onOpenChange={(open) => { if (!open) onClose() }}>
      <Modal.Container size="lg" scroll="inside">
        <Modal.Dialog aria-label={rule ? `Edit rule ${rule.name}` : 'New rule'}>
          <Modal.CloseTrigger />
          <Modal.Header>
            <Modal.Heading className="font-display text-2xl font-bold tracking-tight">
              {rule ? 'Edit rule' : 'New rule'}
            </Modal.Heading>
          </Modal.Header>
          <Modal.Body className="grid gap-7">
            <TextField value={draft.name} onChange={(name) => setDraft((d) => ({ ...d, name }))} isRequired>
              <Label>Name</Label>
              <Input placeholder="e.g. Bank emails" />
            </TextField>

            <Group title="When a message…">
              <CheckboxGroup value={draft.matchers.sources ?? []} onChange={(v) => setM({ sources: v })}>
                <Label>comes from these apps</Label>
                {Object.entries(SOURCE_NAMES).map(([id, label]) => <Check key={id} value={id}>{label}</Check>)}
              </CheckboxGroup>

              {sourceAccounts.length > 1 && (
                <CheckboxGroup value={(draft.matchers.accounts ?? []).map(String)}
                  onChange={(v) => setM({ accounts: v.map(Number) })}>
                  <Label>through these accounts</Label>
                  {sourceAccounts.map((a) => (
                    <Check key={a.id} value={String(a.id)}>{SOURCE_NAMES[a.source] ?? a.source}: {accountLabel(a)}</Check>
                  ))}
                </CheckboxGroup>
              )}

              <CheckboxGroup value={draft.matchers.conversation_kinds ?? []}
                onChange={(v) => setM({ conversation_kinds: v })}>
                <Label>is in</Label>
                {Object.entries(KIND_NAMES).map(([id, label]) => <Check key={id} value={id}>{label}</Check>)}
              </CheckboxGroup>

              <ChipList label="is from these senders" items={draft.matchers.senders ?? []}
                render={(s) => names.senders[s] ?? s}
                onRemove={(s) => setM({ senders: (draft.matchers.senders ?? []).filter((x) => x !== s) })}
                onAdd={(s) => setM({ senders: [...(draft.matchers.senders ?? []), s] })}
                addLabel="Add an email address" />

              <ListField label="is from these email domains" placeholder="mybank.fr, newsletter.example.com"
                value={draft.matchers.sender_domains} onChange={(v) => setM({ sender_domains: v })}
                hint="Subdomains count too: mybank.fr also matches alerts.mybank.fr." />

              {(draft.matchers.conversations ?? []).length > 0 && (
                <ChipList label="is in these conversations" items={(draft.matchers.conversations ?? []).map(String)}
                  render={(c) => names.conversations[c] ?? `Conversation ${c}`}
                  onRemove={(c) => setM({ conversations: (draft.matchers.conversations ?? []).filter((x) => String(x) !== c) })} />
              )}

              <ListField label="contains any of these words" placeholder="unsubscribe, désabonner, promo"
                value={draft.matchers.keywords} onChange={(v) => setM({ keywords: v })}
                hint="Not case or accent sensitive: “ete” matches “Été”." />

              <RadioGroup value={attachmentValue(draft.matchers.has_attachment)} orientation="horizontal"
                onChange={(v) => setM({ has_attachment: v === 'any' ? undefined : v === 'yes' })}>
                <Label>attachments</Label>
                <Choice value="any">Doesn't matter</Choice>
                <Choice value="yes">Has an attachment</Choice>
                <Choice value="no">Has none</Choice>
              </RadioGroup>
            </Group>

            <Group title="Then…">
              <RadioGroup value={draft.outcomes.visibility ?? 'unchanged'}
                onChange={(v) => setO({ visibility: v === 'unchanged' ? undefined : v as Visibility })}>
                <Label>What the agent may read</Label>
                <Choice value="unchanged" description="Other rules decide.">No change</Choice>
                {(['on_request', 'on_request_approval', 'never'] as Visibility[]).map((v) => (
                  <Choice key={v} value={v} description={VISIBILITY_TEXT[v].long}>{VISIBILITY_TEXT[v].short}</Choice>
                ))}
              </RadioGroup>

              <RadioGroup value={draft.outcomes.storage ?? 'store'}
                onChange={(v) => setO({ storage: v === 'store' ? undefined : v as Outcomes['storage'] })}>
                <Label>What Majordome keeps</Label>
                {Object.entries(STORAGE_TEXT).map(([v, label]) => <Choice key={v} value={v}>{label}</Choice>)}
              </RadioGroup>

              <RadioGroup value={String(draft.outcomes.retention_days ?? 'forever')} orientation="horizontal"
                onChange={(v) => setO({ retention_days: v === 'forever' ? undefined : Number(v) })}>
                <Label>How long to keep it</Label>
                {RETENTION.map((r) => <Choice key={r.label} value={String(r.days ?? 'forever')}>{r.label}</Choice>)}
              </RadioGroup>

              <ListField label="Also hide these words or numbers from the agent" placeholder="client number, address"
                value={(draft.outcomes.redact_patterns ?? []).map(unescapeRegex)}
                onChange={(v) => setO({ redact_patterns: v?.map(escapeRegex) })} />
            </Group>

            <PreviewBox preview={preview} checking={checking} problems={problems} />

            {destructive && preview && (
              <Checkbox isSelected={confirmed} onChange={setConfirmed}>
                <Checkbox.Content>
                  <Checkbox.Control><Checkbox.Indicator /></Checkbox.Control>
                  I understand that {destructionText(preview)} This cannot be undone.
                </Checkbox.Content>
              </Checkbox>
            )}
          </Modal.Body>
          <Modal.Footer className="flex flex-wrap justify-between gap-2">
            <div>
              {rule && <Button variant="danger-soft" onPress={remove} isDisabled={busy}>Delete rule</Button>}
            </div>
            <div className="flex gap-2">
              <Button variant="secondary" onPress={onClose}>Cancel</Button>
              <Button variant="primary" onPress={save} isDisabled={!canSave}>
                {busy ? 'Saving…' : rule ? 'Save rule' : 'Create rule'}
              </Button>
            </div>
          </Modal.Footer>
        </Modal.Dialog>
      </Modal.Container>
    </Modal.Backdrop>
  )
}

function PreviewBox({ preview, checking, problems }: { preview: ApplyStats | null; checking: boolean; problems: string[] }) {
  if (problems.length) {
    return (
      <Alert status="warning">
        <Alert.Content>
          <Alert.Title>Not ready yet</Alert.Title>
          <Alert.Description>{problems.join(' ')}</Alert.Description>
        </Alert.Content>
      </Alert>
    )
  }
  if (!preview) return checking ? <Spinner size="sm" /> : null
  return (
    <div aria-live="polite" className="rounded-lg border border-[var(--border)] p-4">
      <p className="font-bold">
        Matches {preview.matched.toLocaleString()} of the {preview.examined.toLocaleString()} messages stored now
        {checking && <Spinner size="sm" className="ml-2 align-middle" />}
      </p>
      {preview.deleted > 0 && <p className="mt-1 text-[var(--danger)]">{plural(preview.deleted, 'message')} would be deleted.</p>}
      {preview.wiped > 0 && <p className="mt-1 text-[var(--danger)]">{plural(preview.wiped, 'message')} would lose their content.</p>}
      {preview.samples && preview.samples.length > 0 && (
        <ul className="mt-3 grid gap-1 text-sm">
          {preview.samples.map((m) => (
            <li key={m.id} className="truncate text-[var(--muted)]">
              <span className="text-[var(--foreground)]">{m.sender ?? m.conversation ?? SOURCE_NAMES[m.source]}</span>: {m.snippet}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

/* ------------------------------------------------------------------------------------------ */
/* Small building blocks                                                                       */
/* ------------------------------------------------------------------------------------------ */

function Group({ title, children }: { title: string; children: ReactNode }) {
  return (
    <fieldset className="grid gap-5">
      <legend className="font-display mb-3 text-lg font-bold tracking-tight">{title}</legend>
      {children}
    </fieldset>
  )
}

function Check({ value, children }: { value: string; children: ReactNode }) {
  return (
    <Checkbox value={value}>
      <Checkbox.Content>
        <Checkbox.Control><Checkbox.Indicator /></Checkbox.Control>
        {children}
      </Checkbox.Content>
    </Checkbox>
  )
}

function Choice({ value, children, description }: { value: string; children: ReactNode; description?: string }) {
  return (
    <Radio value={value}>
      <Radio.Content>
        <Radio.Control><Radio.Indicator /></Radio.Control>
        {children}
      </Radio.Content>
      {description && <Description>{description}</Description>}
    </Radio>
  )
}

/** Comma-separated list field. */
function ListField({ label, value, onChange, placeholder, hint }: {
  label: string; value?: string[]; onChange: (v: string[] | undefined) => void; placeholder?: string; hint?: string
}) {
  const [text, setText] = useState((value ?? []).join(', '))
  return (
    <TextField value={text} onChange={(t) => {
      setText(t)
      const items = t.split(',').map((x) => x.trim()).filter(Boolean)
      onChange(items.length ? items : undefined)
    }}>
      <Label>{label}</Label>
      <Input placeholder={placeholder} />
      {hint && <Description>{hint}</Description>}
    </TextField>
  )
}

function ChipList({ label, items, render, onRemove, onAdd, addLabel }: {
  label: string; items: string[]; render: (item: string) => string
  onRemove: (item: string) => void; onAdd?: (item: string) => void; addLabel?: string
}) {
  const [text, setText] = useState('')
  return (
    <div className="grid gap-2">
      <span className="text-sm font-medium">{label}</span>
      {items.length > 0 && (
        <div className="flex flex-wrap gap-2">
          {items.map((item) => (
            <Chip key={item} className="gap-1 pr-1">
              {render(item)}
              <button type="button" aria-label={`Remove ${render(item)}`} onClick={() => onRemove(item)}
                className="rounded px-1 text-[var(--muted)] hover:text-[var(--foreground)]">×</button>
            </Chip>
          ))}
        </div>
      )}
      {onAdd && (
        <form className="flex gap-2" onSubmit={(e) => { e.preventDefault(); if (text.trim()) { onAdd(text.trim().toLowerCase()); setText('') } }}>
          <TextField value={text} onChange={setText} aria-label={addLabel} className="flex-1">
            <Input placeholder={addLabel} />
          </TextField>
          <Button type="submit" variant="secondary" isDisabled={!text.trim()}>Add</Button>
        </form>
      )}
    </div>
  )
}

/* ------------------------------------------------------------------------------------------ */
/* Helpers                                                                                     */
/* ------------------------------------------------------------------------------------------ */

interface Names { conversations: Record<string, string | null>; senders: Record<string, string | null> }
const EMPTY_NAMES: Names = { conversations: {}, senders: {} }

async function lookupNames(matchers: Matchers[]): Promise<Names> {
  const convs = matchers.flatMap((m) => m.conversations ?? [])
  const senders = matchers.flatMap((m) => m.senders ?? [])
  if (!convs.length && !senders.length) return EMPTY_NAMES
  return api.lookup(convs, senders)
}

function clean<T extends object>(o: T): T {
  return Object.fromEntries(Object.entries(o).filter(([, v]) => v !== undefined && v !== null &&
    !(Array.isArray(v) && v.length === 0) && v !== '')) as T
}

function attachmentValue(v: boolean | null | undefined): string {
  return v === true ? 'yes' : v === false ? 'no' : 'any'
}

function escapeRegex(s: string): string { return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') }
function unescapeRegex(s: string): string { return s.replace(/\\(.)/g, '$1') }

function plural(n: number, word: string): string {
  return `${n.toLocaleString()} ${word}${n === 1 ? '' : 's'}`
}

function accountLabel(a: Account): string {
  if (a.source === 'email') return a.external_id
  const receiver = a.external_id.split('/')[1]
  return receiver ? `+${receiver}` : a.name
}

function destructionText(p: ApplyStats): string {
  const parts = []
  if (p.deleted) parts.push(`${plural(p.deleted, 'stored message')} will be deleted`)
  if (p.wiped) parts.push(`${plural(p.wiped, 'stored message')} will lose their content`)
  return parts.join(' and ') + '.'
}

function summarizeApplied(a: ApplyStats): string {
  const parts = []
  if (a.changed) parts.push(`${plural(a.changed, 'message')} updated`)
  if (a.wiped) parts.push(`${plural(a.wiped, 'message')} emptied`)
  if (a.deleted) parts.push(`${plural(a.deleted, 'message')} deleted`)
  return parts.length ? parts.join(', ') + '.' : 'No stored message was affected.'
}

/** One-sentence summary of a rule, in plain language. */
export function describe(r: RuleDraft, names: Names = EMPTY_NAMES): string {
  const m = r.matchers
  const when: string[] = []
  if (m.sources?.length) when.push(`from ${m.sources.map((s) => SOURCE_NAMES[s] ?? s).join(' or ')}`)
  if (m.conversation_kinds?.length) when.push(`in ${m.conversation_kinds.map((k) => KIND_NAMES[k]?.toLowerCase() ?? k).join(' or ')}`)
  if (m.senders?.length) when.push(`sent by ${m.senders.map((s) => names.senders[s] ?? s).join(', ')}`)
  if (m.sender_domains?.length) when.push(`from ${m.sender_domains.join(', ')}`)
  if (m.conversations?.length) when.push(`in ${m.conversations.map((c) => names.conversations[c] ?? 'a conversation').join(', ')}`)
  if (m.keywords?.length) when.push(`mentioning ${m.keywords.map((k) => `“${k}”`).join(' or ')}`)
  if (m.has_attachment === true) when.push('with an attachment')
  if (m.has_attachment === false) when.push('without attachments')
  if (m.regex) when.push('matching a pattern')
  const o = r.outcomes
  const then: string[] = []
  if (o.storage === 'drop') then.push("don't keep them")
  if (o.storage === 'metadata_only') then.push('keep only who and when')
  if (o.visibility) then.push(VISIBILITY_TEXT[o.visibility].short.toLowerCase())
  if (o.retention_days) then.push(`delete after ${RETENTION.find((x) => x.days === o.retention_days)?.label ?? `${o.retention_days} days`}`)
  if (o.redact_patterns?.length) then.push(`hide ${o.redact_patterns.length} extra term${o.redact_patterns.length > 1 ? 's' : ''}`)
  return `Messages ${when.join(', ') || '(no condition)'}: ${then.join(', ') || 'no change'}.`
}

/** Starting point for "Make a rule from this message". */
export function draftFromMessage(m: { source: string; sender_id: string | null; sender: string | null;
  conversation_id: number; conversation: string | null; conversation_kind: string }): RuleDraft {
  const isEmail = m.source === 'email' && !!m.sender_id
  const domain = isEmail ? m.sender_id!.split('@')[1] : undefined
  return {
    name: isEmail ? `Emails from ${domain}` : `${m.conversation ?? m.sender ?? 'This conversation'}`,
    enabled: true,
    matchers: isEmail ? { sources: ['email'], sender_domains: [domain!] } : { conversations: [m.conversation_id] },
    outcomes: {},
  }
}
