"""Account recovery from the server's shell, for when nobody can sign in.

OpenFeed sends no email, so the ways back into a locked account are: a recovery code (if
two-step verification was on), a reset link from another admin, or this command, run by
whoever controls the server.

Run inside the app container:
    python -m openfeed.tools.accounts list
    python -m openfeed.tools.accounts reset-password USERNAME          # asks for the new password
    python -m openfeed.tools.accounts reset-password USERNAME --link   # prints a one-time reset link
    python -m openfeed.tools.accounts create-admin USERNAME [--display-name NAME]
    python -m openfeed.tools.accounts make-admin USERNAME
    python -m openfeed.tools.accounts reset-2fa USERNAME
    python -m openfeed.tools.accounts enable USERNAME | disable USERNAME
    python -m openfeed.tools.accounts sign-out USERNAME
    python -m openfeed.tools.accounts claim-catalog USERNAME   # upgrade: keep the owner's Home as broad as before

A new password is read from the terminal without echo, or from standard input with
--password-stdin (for scripts); it is never taken as an argument, where it would land in
the shell history and the process list. Every password change and 2FA reset also ends
all of that account's sessions.
"""

from __future__ import annotations

import argparse
import getpass
import secrets
import sys

from .. import accounts, catalog_scope, db, passwords, signin
from ..config import get_settings


def _read_password(args: argparse.Namespace, username: str) -> str:
    if args.password_stdin:
        value = sys.stdin.readline().rstrip("\n")
    else:
        value = getpass.getpass("New password: ")
        if getpass.getpass("Repeat it: ") != value:
            raise SystemExit("The two passwords don't match. Nothing was changed.")
    try:
        return passwords.check_policy(value, username=username)
    except passwords.PasswordPolicyError as exc:
        raise SystemExit(f"{exc.message} Nothing was changed.") from exc


def _account(username: str) -> dict:
    account = accounts.get_by_username(username)
    if not account:
        raise SystemExit(f"No account is called {username!r}. `list` shows them.")
    return account


def cmd_list(args: argparse.Namespace) -> None:
    rows = accounts.list_accounts()
    if not rows:
        print("No accounts yet. Open /setup, or run: python -m openfeed.tools.accounts create-admin USERNAME")
        return
    for row in rows:
        flags = [row["role"]]
        if row.get("disabled"):
            flags.append("disabled")
        if row.get("totp_enabled_at"):
            flags.append("2fa")
        methods = ",".join(row.get("methods") or []) or "no sign-in method"
        print(f"{row['username']:<24} {' '.join(flags):<22} {methods:<28} active {row.get('last_active_at') or 'never'}")


def cmd_reset_password(args: argparse.Namespace) -> None:
    account = _account(args.username)
    if args.link:
        token = secrets.token_urlsafe(32)
        accounts.create_link("reset", signin.token_hash(token), 24 * 3600, account_id=account["id"])
        print(f"One-time reset link for {account['username']} (24 hours):")
        print(f"{get_settings().public_url}/auth/reset/{token}")
        return
    password = _read_password(args, account["username"])
    accounts.set_password(account["id"], passwords.hash_password(password))
    ended = accounts.delete_sessions(account["id"])
    print(f"Password set for {account['username']}. {ended} session(s) ended.")


def cmd_create_admin(args: argparse.Namespace) -> None:
    username = accounts.clean_username(args.username)
    password = _read_password(args, username)
    try:
        account = accounts.create_account(username, args.display_name or "", role="admin", password_hash=passwords.hash_password(password))
    except accounts.AccountError as exc:
        raise SystemExit(exc.message) from exc
    signin.forget_setup_code()
    print(f"Admin account {account['username']} created. Sign in at {get_settings().public_url}/auth/login")


def cmd_make_admin(args: argparse.Namespace) -> None:
    account = _account(args.username)
    accounts.update_account(account["id"], role="admin", disabled=False)
    accounts.delete_sessions(account["id"])
    print(f"{account['username']} is an admin now (and enabled). Their sessions ended; they sign in again.")


def cmd_reset_2fa(args: argparse.Namespace) -> None:
    account = _account(args.username)
    accounts.disable_totp(account["id"])
    ended = accounts.delete_sessions(account["id"])
    print(f"Two-step verification is off for {account['username']}. {ended} session(s) ended.")


def cmd_enable(args: argparse.Namespace) -> None:
    account = _account(args.username)
    accounts.update_account(account["id"], disabled=False)
    print(f"{account['username']} can sign in again.")


def cmd_disable(args: argparse.Namespace) -> None:
    account = _account(args.username)
    try:
        accounts.update_account(account["id"], disabled=True)
    except accounts.AccountError as exc:
        raise SystemExit(exc.message) from exc
    print(f"{account['username']} is disabled and signed out everywhere; their API tokens stopped working.")


def cmd_claim_catalog(args: argparse.Namespace) -> None:
    """Give one account the catalog as it stands: every video stored so far.

    An install that already had several people when scoping arrived claims nothing, because
    nobody can tell who built it. Run this once for the person whose install it really was,
    so their Home keeps its breadth. Other accounts are not affected."""
    account = _account(args.username)
    with db.connect() as conn:
        with conn.cursor() as cur:
            top = catalog_scope.claim_catalog(cur, str(account["user_key"]))
        conn.commit()
    catalog_scope.clear_cache()
    if top:
        print(f"{account['username']} now has every video stored so far (through id {top}).")
    else:
        print("The catalog is empty. Nothing to claim.")


def cmd_sign_out(args: argparse.Namespace) -> None:
    account = _account(args.username)
    print(f"{accounts.delete_sessions(account['id'])} session(s) ended for {account['username']}.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m openfeed.tools.accounts", description="Manage OpenFeed accounts from the server shell.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="list accounts").set_defaults(func=cmd_list)

    reset = sub.add_parser("reset-password", help="set a new password, or print a one-time reset link")
    reset.add_argument("username")
    reset.add_argument("--link", action="store_true", help="print a reset link instead of asking for a password")
    reset.add_argument("--password-stdin", action="store_true", help="read the new password from standard input")
    reset.set_defaults(func=cmd_reset_password)

    create = sub.add_parser("create-admin", help="create a new admin account")
    create.add_argument("username")
    create.add_argument("--display-name", default="")
    create.add_argument("--password-stdin", action="store_true")
    create.set_defaults(func=cmd_create_admin)

    for name, func, text in (
        ("make-admin", cmd_make_admin, "give an existing account the admin role"),
        ("reset-2fa", cmd_reset_2fa, "turn off two-step verification for an account"),
        ("enable", cmd_enable, "let a disabled account sign in again"),
        ("disable", cmd_disable, "stop an account from signing in"),
        ("sign-out", cmd_sign_out, "end every session of an account"),
        ("claim-catalog", cmd_claim_catalog, "give an account every video stored so far (for an install that already had several people)"),
    ):
        item = sub.add_parser(name, help=text)
        item.add_argument("username")
        item.set_defaults(func=func)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    db.init_db()
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
