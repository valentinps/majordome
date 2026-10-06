"""Admin CLI: `majordome set-password`, `majordome email-auth <address>`."""

import argparse
import getpass

from argon2 import PasswordHasher

from majordome.config import settings


def set_password() -> None:
    pw = getpass.getpass("New web UI password: ")
    if len(pw) < 12 or pw != getpass.getpass("Repeat: "):
        raise SystemExit("passwords differ or shorter than 12 characters")
    print("Put this in the env file as MAJORDOME_UI_PASSWORD_HASH (single-quoted):")
    print(PasswordHasher().hash(pw))


def email_auth(address: str) -> None:
    from majordome.ingest import oauth
    from majordome.ingest.imap import load_accounts

    acc = next((a for a in load_accounts(settings()) if a.address.lower() == address.lower()), None)
    if acc is None or acc.auth != "oauth_microsoft" or not acc.client_id:
        raise SystemExit(f"{address} is not an oauth_microsoft account with a client_id")
    oauth.device_login(settings(), acc.address, acc.client_id)


def main() -> None:
    p = argparse.ArgumentParser(prog="majordome")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("set-password")
    ea = sub.add_parser("email-auth")
    ea.add_argument("address")
    args = p.parse_args()
    if args.cmd == "set-password":
        set_password()
    else:
        email_auth(args.address)


if __name__ == "__main__":
    main()
