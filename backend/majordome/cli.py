"""Admin CLI: set the web UI password, add email accounts, log in to Outlook (OAuth)."""

import argparse
import getpass
import json
import os
from pathlib import Path

from argon2 import PasswordHasher

from majordome.config import settings

PROVIDERS = {
    "gmail": {"host": "imap.gmail.com", "auth": "password"},
    "outlook": {"host": "outlook.office365.com", "auth": "oauth_microsoft"},
}


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text)
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def set_password() -> None:
    pw = getpass.getpass("New web UI password (12+ characters): ")
    if len(pw) < 12 or pw != getpass.getpass("Repeat: "):
        raise SystemExit("Passwords differ or are shorter than 12 characters. Nothing changed.")
    _write_private(settings().ui_password_hash_file, PasswordHasher().hash(pw) + "\n")
    print(f"Password set ({settings().ui_password_hash_file}).")


def add_email() -> None:
    path = settings().email_accounts_file
    accounts = json.loads(path.read_text()) if path.exists() else []
    provider = input(f"Provider ({' / '.join(PROVIDERS)} / other): ").strip().lower()
    address = input("Email address: ").strip()
    name = input(f"Display name [{provider.title() or address}]: ").strip() or provider.title()
    acc: dict = {"name": name, "address": address, "folders": ["INBOX"]}
    if provider in PROVIDERS:
        acc.update(PROVIDERS[provider])
    else:
        acc["host"] = input("IMAP host: ").strip()
        acc["auth"] = "password"
    if acc["auth"] == "password":
        acc["password"] = getpass.getpass("App password (hidden): ").replace(" ", "")
    else:
        acc["client_id"] = input("Application (client) ID of your Entra app: ").strip()
    accounts = [a for a in accounts if a["address"].lower() != address.lower()] + [acc]
    _write_private(path, json.dumps(accounts, indent=2))
    print(f"Saved {address}.")
    if acc["auth"] == "oauth_microsoft":
        print(f"Next: run `majordome email-auth {address}` to sign in.")


def email_auth(address: str) -> None:
    from majordome.ingest import oauth
    from majordome.ingest.imap import load_accounts

    acc = next((a for a in load_accounts(settings()) if a.address.lower() == address.lower()), None)
    if acc is None or acc.auth != "oauth_microsoft" or not acc.client_id:
        raise SystemExit(f"{address} is not an Outlook account with a client ID; run add-email")
    oauth.device_login(settings(), acc.address, acc.client_id)


def set_webhook() -> None:
    from majordome.notify.discord import WEBHOOK

    url = getpass.getpass("Discord webhook URL (hidden): ").strip()
    if not WEBHOOK.match(url):
        raise SystemExit("That doesn't look like a Discord webhook URL "
                         "(https://discord.com/api/webhooks/...). Nothing changed.")
    _write_private(settings().discord_webhook_file, url + "\n")
    print("Webhook saved. Majordome will post important messages there.")


def test_webhook() -> None:
    import asyncio

    from majordome.notify.discord import load_webhook, send

    url = load_webhook(settings().discord_webhook_file)
    if not url:
        raise SystemExit("No webhook set; run set-webhook first.")
    print(asyncio.run(send(url, "Majordome is connected. Important messages will appear here.")))


def eval_injection() -> None:
    """Run the prompt-injection corpus through the real triage model and show its verdicts.

    Nothing is stored and nothing is sent: this only shows how the model judges hostile input.
    """
    import asyncio
    import json
    from datetime import UTC, datetime
    from pathlib import Path

    from majordome.agent import triage
    from majordome.llm import provider_from
    from majordome.store.models import Account, Conversation, Identity, Message

    cfg = settings()
    corpus = json.loads((Path(triage.__file__).parent / "injection_corpus.json").read_text("utf-8"))
    batch = []
    for i, case in enumerate(corpus):
        m = Message(id=-i, sent_at=datetime.now(UTC), subject=case.get("subject"),
                    body_agent=case["text"], raw={})
        m.account = Account(source=case["source"], name=case["source"], external_id="eval")
        m.conversation = Conversation(kind="dm", title=case["sender"], external_id="eval")
        m.sender = Identity(source=case["source"], external_id=case["sender"],
                            display_name=case["sender"])
        m.attachments = []
        batch.append(m)

    async def run() -> None:
        res = await provider_from(cfg).complete(
            triage.SYSTEM_PROMPT, triage.build_input(batch, datetime.now(UTC)), triage.SCHEMA,
            cfg.triage_model)
        verdicts = triage.parse_output(res.output, len(batch))
        print(f"model {res.model}, {res.duration_ms} ms, {len(verdicts)}/{len(batch)} valid\n")
        for i, case in enumerate(corpus, start=1):
            v = verdicts.get(i)
            if v is None:
                print(f"{case['id']:<24} NO VALID VERDICT")
                continue
            push = triage.notification_kind(v, cfg.scam_threshold) or "-"
            print(f"{case['id']:<24} {v.category:<13} risk={v.scam_risk:.2f} "
                  f"{v.importance:<9} push={push:<13} | {v.summary}")

    asyncio.run(run())


def reapply_policy() -> None:
    """Re-evaluate every stored message against the current rules (e.g. after an upgrade)."""
    import asyncio

    from majordome.policy.apply import reapply
    from majordome.store import db

    async def run() -> None:
        async with db.session() as s:
            st = await reapply(s)
            await s.commit()
        print(f"Examined {st.examined}, updated {st.changed}, emptied {st.wiped}, "
              f"deleted {st.deleted}.")

    asyncio.run(run())


def main() -> None:
    p = argparse.ArgumentParser(prog="majordome")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("set-password", help="set the web UI password")
    sub.add_parser("add-email", help="add or replace an email account")
    sub.add_parser("reapply-policy", help="re-evaluate stored messages against the rules")
    sub.add_parser("set-webhook", help="set the Discord webhook for notifications")
    sub.add_parser("test-webhook", help="post a test notification to Discord")
    sub.add_parser("eval-injection", help="show how the real model judges hostile messages")
    ea = sub.add_parser("email-auth", help="sign in to an Outlook account (OAuth)")
    ea.add_argument("address")
    args = p.parse_args()
    if args.cmd == "set-password":
        set_password()
    elif args.cmd == "add-email":
        add_email()
    elif args.cmd == "reapply-policy":
        reapply_policy()
    elif args.cmd == "set-webhook":
        set_webhook()
    elif args.cmd == "test-webhook":
        test_webhook()
    elif args.cmd == "eval-injection":
        eval_injection()
    else:
        email_auth(args.address)


if __name__ == "__main__":
    main()
