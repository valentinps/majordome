# Majordome

A self-hosted, single-user butler for your messages. Majordome collects everything you receive
(email, WhatsApp, Discord, Android notifications, …) into one private store, and an LLM agent
watches it for you — so you stop monitoring your apps, and it reaches out only when something
matters or when you asked it to.

> **Status:** early development (milestone M0 — infrastructure). Nothing usable yet.

- Read-only by default; outbound messaging is opt-in and always requires manual confirmation.
- Deterministic rules decide what is stored, what the agent may see, and what is redacted.
- Agents have no tools that can send data anywhere; see [docs/PLAN.md](docs/PLAN.md) §8.

See [docs/PLAN.md](docs/PLAN.md) for the full design and roadmap.

## License

[AGPL-3.0](LICENSE)
