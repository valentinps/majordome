# Majordome — Project Plan

> A self-hosted, single-user "butler" for your messages. Majordome collects everything you receive
> (email, WhatsApp, Discord, Android notifications, …) into one private store, and an LLM agent
> watches it for you, so you can stop monitoring your apps. It reaches out only when something
> matters or when you asked it to.

License: **AGPL-3.0**

---

## 1. Goals and non-goals

### Goals
- **Unified ingestion** of messages and notifications from many apps and multiple accounts.
- **Archive and search**: a single searchable history across all selected sources.
- **An agent that triages for you**: it sorts messages, flags spam and scams, and decides what deserves your attention.
- **Standing instructions ("watchers")**: for example, *"Tell me when the email saying my washing machine is done arrives."*
- **You keep deterministic control** over what is stored, what the agent can see, and what gets redacted.
- **Ready for local LLMs**: cloud LLM during development, local models later.
- **Open source** and reasonably easy for others to self-host.

### Non-goals (v1)
- Replacing the original apps as a full client.
- Sending messages. v1 is **read-only**. Outbound messaging is designed in but off by default
  (see §9).
- Supporting multiple users.
- A public internet deployment. The stack is reachable only over a private network (Tailscale).

---

## 2. Key decisions (from the design discussion)

| Topic | Decision |
|---|---|
| Chat app ingestion | **Matrix + mautrix bridges** (WhatsApp, Discord to start). We don't write chat connectors. |
| Email ingestion | **Direct IMAP** (IDLE), not bridged. We keep headers (SPF/DKIM/Received) for scam detection. |
| Android | **Kotlin notification forwarder** (`NotificationListenerService`), as a catch-all for apps without bridges. |
| Backend | **Python** (FastAPI, SQLAlchemy 2, Alembic, mautrix-python). |
| Web UI | **TypeScript** (React + Vite). The backend serves it as static files. |
| Database | **PostgreSQL + pgvector**, using Postgres full-text search. Postgres is also the job queue (no Redis). |
| Homeserver | **Synapse** (reference implementation, best bridge compatibility). Federation is disabled. |
| LLM | Provider abstraction per role. Dev uses **Claude Code headless with the user's subscription** (personal use only). API and local providers come later. |
| Talking to the agent | Web UI chat (v1). |
| Push to user | Discord webhook (v1). Pluggable channels later. |
| Agent scheduling | Configurable: **real-time** per message, or **batched** every N minutes. |
| Retention | **Keep by default**. Retention rules can expire data. |
| Hosting | Co-hosted on the existing Hetzner box (Ubuntu 26.04, 12 cores, 64 GB RAM, no GPU), isolated (see §8). |
| Network access | **Tailscale only**. Nothing is exposed publicly. |

---

## 3. Architecture overview

```
                         ┌──────────────── Hetzner server (Docker network "majordome") ───────────────┐
 WhatsApp ─┐             │                                                                            │
 Discord  ─┼─► mautrix ──┼─► Synapse ──(client API)──┐                                                │
 (later…) ─┘   bridges   │                           │                                                │
                         │                           ▼                                                │
 IMAP accounts ──────────┼──────────────────► [ingest workers] ──► normalize ─► dedup ─► POLICY ─┐    │
                         │                           ▲                       (storage/redaction) │    │
 Android phone ──(Tailscale HTTPS)──► [ingest API] ──┘                                           ▼    │
                         │                                                              [ Postgres ]  │
                         │                                                              messages,     │
                         │                                                              embeddings,   │
                         │                                                              rules, audit  │
                         │                                                                   │        │
                         │      ┌────────────────────────────────────────────────────────────┤        │
                         │      ▼                                                            ▼        │
                         │ [triage worker] ─► prefilter ─► TRIAGE LLM (no tools, JSON) ─► decide      │
                         │                                                                   │        │
                         │ [web UI + API] ◄─► QUERY LLM (policy-filtered read tools)         ▼        │
                         │        ▲                                                   [notifier]      │
                         └────────┼──────────────────────────────────────────────────────┼────────────┘
                                  │ Tailscale                                            │ fixed destination
                               You (PC / phone browser)                         Discord webhook / UI inbox
```

### Processes (containers)
1. **synapse**: private homeserver (no federation, registration disabled).
2. **mautrix-whatsapp** and **mautrix-discord**: bridges, registered as appservices.
3. **postgres**: one instance with separate databases for Synapse, the bridges, and Majordome.
4. **majordome-api**: FastAPI app. Hosts the web UI, the REST API, the Android ingest endpoint, and the query agent.
5. **majordome-worker**: the Matrix listener, IMAP IDLE listeners, the embedding job, the triage scheduler, the notifier, and retention jobs.
6. **majordome-llm**: an isolated container that runs the LLM provider (Claude Code CLI for now). Its network egress is restricted to the LLM endpoint.

---

## 4. Ingestion

### 4.1 Matrix (bridged chat apps)
- Majordome logs in as the **bridge owner Matrix user** (e.g. `@me:majordome.internal`). It
  follows every portal room the bridges create.
- **Bridge config**:
  - Turn off end-to-end encryption in bridge rooms. The homeserver is private and data is protected at rest instead.
  - Turn off presence and typing forwarding.
  - Enable backfill for new messages only. History starts "from now".
- **Majordome never sends read receipts, typing notifications, or presence.** Otherwise your
  contacts would see "read" for messages you never saw. There will be an automated test for this.
- Room metadata from the bridges (the network, the remote chat ID, the sender's remote ID) is
  mapped onto the normalized model.
- The listener checkpoints its sync token, so it can resume after a restart without losing or duplicating messages.

### 4.2 Email (IMAP)
- One IDLE connection per account, with periodic full sync as a fallback.
- Auth uses app passwords at first. OAuth2 for Gmail/Outlook is a later improvement.
- The worker stores the parsed text body (HTML converted to text), attachment metadata, and the full headers. SPF/DKIM/DMARC results are signals for scam detection.
- Mail on the server is never modified. No flags are set and nothing is moved: strictly read-only (`EXAMINE`, not `SELECT`).

### 4.3 Android notification forwarder
- A small Kotlin app using `NotificationListenerService`.
- **An on-device allowlist decides which apps are forwarded.** Data minimization starts on the phone.
- It sends the package name, app label, title, text, big text, conversation info if present, the timestamp, and the notification key.
- A local queue (WorkManager) handles offline periods. The app POSTs to `/ingest/android` over Tailscale with a per-device token.
- It is distributed as an APK (and maybe on F-Droid later). It is not planned for the Play Store.

### 4.4 Deduplication
- The same WhatsApp message can arrive through both the bridge and an Android notification.
- Each source has an **ingestion priority**. For apps that also have a bridge, notifications are **fallback only**.
- Matching uses source app, normalized sender, normalized text, and a time window of about ±2 minutes. A notification with no match after the window is kept, because it may mean the bridge is down. That condition is itself reported as a health signal.

---

## 5. Data model (initial)

| Table | Purpose |
|---|---|
| `source` | A type of integration (`whatsapp`, `discord`, `email`, `android:<package>`). |
| `account` | A concrete account on a source (e.g. 2 email accounts). |
| `conversation` | A chat, channel, group, or email thread. |
| `contact` | One real person or entity, unified across platforms. |
| `identity` | A handle on a platform (phone number, email address, Discord ID) → `contact`. |
| `message` | The normalized message (fields below). |
| `attachment` | File metadata plus storage path. Extracted text (OCR/transcription) is added later. |
| `rule` | A policy rule (see §6). |
| `watcher` | A standing instruction from the user (see §7.3). |
| `triage_result` | The agent's structured verdict per message, or per batch. |
| `notification` | What was sent to the user, when, through which channel, and why. |
| `approval_request` | A pending user approval (per-access visibility, watcher creation, and outbound later). |
| `audit_log` | An append-only record of agent inputs and outputs and of policy decisions. |
| `job` | A Postgres job queue (`SELECT … FOR UPDATE SKIP LOCKED`). |

**Message fields**:
- `id`, `account_id`, `conversation_id`, `sender_identity_id`, `direction` (in/out), `sent_at`, `received_at`
- `ingest_channel` (matrix / imap / android)
- `body_text`: the original text, kept if the storage policy allows
- `body_agent`: the redacted text, which is the only version an LLM ever sees
- `subject` (email only), `raw` (jsonb, the source payload), `dedup_key`
- `visibility` (resolved at ingestion, re-resolved at access time)
- `fts` (tsvector), `embedding` (pgvector)

**Text search for mixed French/English**: use the `simple` dictionary plus `unaccent`. This avoids
picking a single language stemmer. Embeddings handle the semantic side.

---

## 6. Policy engine (deterministic control)

This is the core trust layer. **It is plain code with no LLM, and it is fully unit-tested.** It is
enforced at ingestion *and* inside every data-access function the agents use. Prompts never enforce it.

### 6.1 Dimensions
Each message has four independent outcomes:

| Dimension | Values |
|---|---|
| **Storage** | `store` · `metadata_only` (sender, time, source, no body) · `drop` |
| **Agent visibility** | `always` (triage and query) · `on_request` (query agent only, when you ask) · `on_request_approval` (query agent must get a per-access approval from you) · `never` |
| **Redaction** | A set of redactors applied to `body_agent`: OTP/2FA codes, card numbers, IBAN, passwords and reset links, custom regex |
| **Retention** | `forever` (default) · `N days` · `delete body after N days, keep metadata` |

### 6.2 Rules
- A rule is **matchers → outcomes** and has a priority (used only for ordering in the UI).
- **Matchers**: source, account, conversation, contact, sender identity, sender domain, keyword
  or regex in subject/body, has attachment, Android package, and boolean combinations of these.
- **Conflict resolution: the most restrictive outcome wins** in each dimension, independently. Deny overrides allow, `never` beats `on_request`, and the shortest retention wins.
- **Defaults**: `store`, `always`, the built-in redactors enabled, `forever`.
- When a rule changes, existing messages are re-evaluated. Visibility is always resolved at access time anyway. Storage and retention changes become a background job.
- Rules are managed **only through the web UI or API by the user**. Agents can never create or modify rules.
- A **"why?" view** shows, for any message, which rules applied and what the outcome was.

### 6.3 Tests
- Table-driven unit tests and property-based tests (Hypothesis). One example property: "adding a rule can never make a message *more* visible".
- Redactor tests use real-world samples of OTP SMS, bank emails, and password reset emails, in French and English.

---

## 7. The agent

### 7.1 LLM providers
- The interface is roughly `complete(role, system, input, json_schema) -> validated JSON | text`.
- Each **role** gets its own provider: `triage`, `query`, `watcher_compiler`, `embeddings`.
- Providers:
  1. **`claude_code`** (dev, personal use): runs `claude -p` headless with the user's subscription.
     - **All built-in tools disabled** (Bash, file read/write, WebFetch, …).
     - **No MCP.** It uses a strict, empty MCP config, so connectors attached to the claude.ai account are not available.
     - A dedicated, empty `CLAUDE_CONFIG_DIR` and a long-lived token from `claude setup-token`. It never uses root's existing login.
     - It runs as a non-root user in the `majordome-llm` container, with egress allowed only to Anthropic.
     - The exact CLI flags must be checked against the current Claude Code version in M0, and tested: the provider must fail closed if the tools are not disabled.
     - **Documented as personal-use only.** Other users configure an API key or a local model. Anthropic does not allow third-party products to offer subscription login.
  2. **`anthropic_api`**: API key.
  3. **`openai_compatible`**: covers Ollama, llama.cpp server, vLLM, and other providers.
- **Embeddings run locally on the CPU from day one.** A small multilingual model (e.g. multilingual-e5-small or bge-m3) costs nothing and keeps message content on the box.
- The subscription has usage limits shared with your own Claude Code usage, so the default is **batch mode**, with aggressive deterministic pre-filtering.

### 7.2 Triage agent (sees untrusted content, so it has no power)
- Input: a batch of new messages with `visibility = always` (redacted `body_agent` only), the list of active watchers, and the user's preferences profile.
- **It has no tools.** Its output must match a strict JSON schema, for example:
  ```json
  {
    "results": [{
      "message_id": "…",
      "category": "personal|work|transactional|newsletter|notification|spam|scam|other",
      "scam_risk": 0.0,
      "importance": "interrupt|digest|silent",
      "matched_watchers": ["watcher_id"],
      "summary": "≤ 200 chars, plain text"
    }]
  }
  ```
- It has **no free text that reaches a destination unchecked**. Summaries are sanitized and length-capped (§8.2).
- **Pre-filters run before the LLM**, to save tokens:
  - Rules ("never notify for newsletters from X").
  - Cheap embedding or keyword prefilters for watchers.
  - Conversations you muted.
  - Large-group heuristics.
- Mode is configurable: real-time (each message as it arrives), batched (every N minutes), or both (real-time only for some sources).

### 7.3 Watchers (standing instructions)
- You type in the chat: *"I'm expecting an email saying my washing machine is done, notify me."*
- The **watcher compiler** turns this into a structured `watcher`:
  - description, match hints (sources, senders, keywords), match semantics for the LLM
  - one-shot or recurring, expiry, importance, notification style
- The UI shows the compiled watcher, and **you confirm it** before it becomes active.
- Watchers can be listed, edited, paused, and deleted in the UI. One-shot watchers close after they fire.
- When the triage agent reports a match, the notifier sends a short message, e.g. *"🧺 Washing machine
  done (email from Bosch, 14:32)."*

### 7.4 Query agent (driven by you, read tools only)
- It is triggered **only by your messages** in the web UI chat.
- **Tools** (all of them call policy-filtered code, never raw SQL):
  - `search_messages(query, sources?, contacts?, date_range?, limit)`: hybrid full-text + vector search, visibility ≥ `on_request`.
  - `get_conversation(conversation_id, around?, limit)`
  - `get_message(message_id)`
  - `list_watchers()`
  - `propose_watcher(…)`: creates an **approval request**. Nothing changes without your click.
  - `request_access(message_ids, reason)`: for `on_request_approval` messages. It creates an approval request and blocks until you allow or deny.
- Search results are untrusted content too. That's why every tool that changes state is **propose-only**, and why rules are out of reach for this agent.
- Answers **cite messages** (with links into the UI timeline).

### 7.5 Later agent features (backlog)
- A digest ("3 things need a reply, 2 deliveries, 1 likely scam…"), scheduled or on demand.
- Feedback ("not important", "always tell me about this sender"), which updates a preferences profile and suggests rules.
- Unifying contacts across platforms, with merges suggested by the agent and confirmed by you.
- Processing attachments: OCR (images, PDFs) and voice-note transcription (Whisper on CPU).
- Extraction: appointments (calendar suggestions), parcel tracking, bills and due dates.
- "Waiting on reply" tracking.
- Drafting replies (only once outbound exists, §9).

---

## 8. Security model

### 8.1 Threat model highlights
- **Prompt injection through incoming messages** is expected and treated as normal.
- **Data leaks.** An attack needs three things at once: private data, untrusted input, and an outbound channel. We remove the outbound channel structurally.
- **Server compromise.** The box also hosts public game servers (Pelican/Wings run as root). A root compromise would expose the whole message history and the bridge sessions.

### 8.2 Controls
1. **No egress tools for any agent.** The only network call is the LLM request, made by our code.
2. **Notification destinations are fixed in config.** The agent chooses *what* to say, never *where*.
3. **Output sanitization** on every LLM output that reaches a channel or the UI:
   - Strip markdown images and HTML.
   - Neutralize URLs unless they appear verbatim in the cited source message.
   - Discord webhooks use `flags: SUPPRESS_EMBEDS` and `allowed_mentions: {parse: []}`.
   - The web UI uses a strict CSP and renders agent text as text, not HTML.
4. **Network isolation.** The `majordome-llm` container can only reach the LLM endpoint. Other
   containers have no internet access, except the bridges, the IMAP worker, and the notifier (Discord host only).
5. **The privilege split** described in §7. The triage agent (untrusted input) has no tools. The query
   agent (driven by you) has read tools only, and every state change goes through an approval.
6. **Provenance in notifications.** They always show the source app, the sender, and the scam flag. The agent must not lend its credibility to message content: it reports, it doesn't vouch.
7. **Audit log** of every LLM call (input IDs, output, provider, tokens) and every policy decision.
8. **Prompt-injection regression suite.** A corpus of hostile messages (exfiltration attempts,
   fake system prompts, URL tricks, attempts to create watchers or rules, in FR and EN) runs in CI against the
   triage and query pipelines. It asserts that no forbidden effects happen.

### 8.3 Hosting hardening (co-hosting on the game box)
- A dedicated Linux user `majordome` and a dedicated directory `/opt/majordome`, with rootless containers where practical.
- A dedicated Docker network. **Ports bind only to the Tailscale interface.** Ports 80/443 stay with Pelican.
- **Encryption at rest** for data volumes (an encrypted volume via LUKS or gocryptfs). Note: if the key is on the same machine, this protects backups, disk disposal, and casual access, not a live root compromise.
- **Encrypted off-box backups** (restic).
- **Web UI auth** even on the tailnet: a single-user password plus a session, with TOTP optional.
- **Planned migration path.** Moving to a dedicated VPS later should only mean moving the compose stack and the volumes. The design must not rely on co-location.

---

## 9. Outbound messaging (designed in, not in v1)
- **Off by default.** It is enabled per app and per account by the user.
- It is exposed to the agent as an **outbound skill** that only **creates a draft plus an approval request**.
  Nothing is ever sent automatically.
- The confirmation screen is rendered by our code from the exact payload: recipient, account, and
  text. It is never a description written by the LLM. Confirmation is a UI action, never a chat reply like "yes".
- There is an allowlist of recipients and rate limits for each skill.
- Sending is done through the Matrix bridge (posting in the portal room) or SMTP for email.

---

## 10. Notification channels
- A `Channel` interface with methods `send(notification)` and, optionally, `receive()` for two-way channels later.
- v1 channels:
  - **Discord webhook** (push only, embeds suppressed, mentions disabled)
  - **Web UI inbox**
- Later: ntfy (self-hosted push), a Matrix room, a Telegram or Discord bot (two-way chat with the agent), and possibly Apprise.
- Delivery rules: importance → channel, quiet hours, exceptions by contact ("family always gets through").

---

## 11. Web UI (v1 scope)
- **Unified timeline** with filters (source, account, contact, category, scam flag, importance).
- **Message detail**, including the "why?" panel (rules applied, triage verdict, watcher matches).
- **Agent chat** (query agent and watcher creation).
- **Watchers**: list, edit, pause, delete.
- **Rules editor** with a preview ("this rule would affect 132 existing messages").
- **Approvals** queue.
- **Health**: bridge status, IMAP status, Android device last seen, LLM usage, queue depth.
- **Settings**: accounts, channels, LLM providers per role, triage mode, retention.

---

## 12. Repository layout (proposed)

```
majordome/
├── LICENSE                      # AGPL-3.0
├── README.md
├── docs/
│   ├── PLAN.md                  # this file
│   ├── SECURITY.md              # threat model and controls
│   └── deploy.md
├── deploy/
│   ├── docker-compose.yml
│   ├── synapse/                 # homeserver.yaml template
│   ├── bridges/                 # mautrix-whatsapp / mautrix-discord config templates
│   └── llm/                     # isolated LLM container (Claude Code provider)
├── backend/                     # Python package "majordome"
│   ├── majordome/
│   │   ├── api/                 # FastAPI routes (UI API, ingest, auth)
│   │   ├── ingest/              # matrix, imap, android, normalize, dedup
│   │   ├── policy/              # rule engine, redactors, retention
│   │   ├── store/               # models, migrations (Alembic), search
│   │   ├── agent/               # providers, triage, query, watchers, sanitizer
│   │   ├── notify/              # channels, delivery rules
│   │   └── jobs/                # queue, scheduler
│   └── tests/
│       ├── policy/
│       ├── injection_corpus/
│       └── …
├── web/                         # React + Vite + TS
└── android/                     # Kotlin notification forwarder
```

---

## 13. Milestones

### M0: Infrastructure and foundations
- Repo, license, CI (lint, type check, tests).
- Tailscale on the server, the PC, and the phone.
- Compose stack on the server: Postgres, Synapse (no federation), mautrix-whatsapp, mautrix-discord.
  Log in both bridges and check that portal rooms appear.
- Check that read receipts and presence are disabled.
- Spike: run the Claude Code CLI headless in an isolated container with all tools and MCP disabled and only Anthropic egress allowed. Confirm structured JSON output and measure latency.
- **Done when**: WhatsApp and Discord messages show up in Synapse rooms, and a sandboxed `claude -p`
  call returns valid JSON with no tools available.

### M1: Ingestion and archive
- Matrix listener, IMAP IDLE for 2 accounts, normalized model, migrations.
- Full-text search, basic web UI timeline and message detail, single-user auth.
- **Done when**: messages from WhatsApp, Discord, and both mailboxes appear in one timeline and can be searched.

### M2: Policy engine
- Storage, visibility, redaction, and retention rules, plus redactors and the "why?" view.
- Rules editor with a preview, and a retention job.
- Full unit and property test suite.
- **Done when**: rules control storage and visibility deterministically and the test suite passes.

### M3: Triage and notifications
- LLM provider abstraction and the `claude_code` provider.
- Triage agent (batch and real-time modes), spam and scam scoring, importance.
- Output sanitizer, Discord webhook channel, UI inbox, audit log.
- First version of the prompt-injection corpus in CI.
- **Done when**: important messages trigger a Discord notification, scams are flagged, and the
  injection corpus produces no forbidden effects.

### M4: Watchers and agent chat
- Local embeddings and hybrid search.
- Query agent with read tools and citations.
- Watcher compiler, then approval, then matching in triage, then the notification. One-shot and recurring watchers, with expiry.
- `on_request_approval` access flow.
- **Done when**: the washing-machine scenario works end to end.

### M5: Android forwarder
- Kotlin app (allowlist, offline queue, device token), ingest endpoint, dedup against bridges.
- **Done when**: allowlisted app notifications arrive, and WhatsApp duplicates are merged.

### M6: Quality of life
- Digests, feedback learning, contact unification, delivery rules and quiet hours.
- Attachment OCR and voice-note transcription, extraction (appointments, parcels, bills).

### M7: Local LLMs
- `openai_compatible` provider (Ollama / llama.cpp), CPU-sized triage model benchmarks on the
  Hetzner box, per-role provider routing.

### Later
- Outbound skills (§9), more bridges (Signal, Telegram, Google Messages, Instagram, …), more
  channels (ntfy, two-way bots), and docs and packaging so others can self-host.

---

## 14. Open questions / to verify
- Exact Claude Code CLI flags to disable all tools and MCP and get schema-constrained output (M0 spike).
- Gmail and Outlook: are app passwords enough, or is OAuth2 needed right away?
- Which encryption-at-rest mechanism to use for the volumes on the existing RAID setup.
- Embedding model choice: check FR/EN quality against CPU speed on the box.
- Should `metadata_only` messages still be matchable by watchers (sender and subject only)?
- Synapse resource footprint vs. a lighter homeserver. Synapse is the default for bridge compatibility and can be revisited later.
