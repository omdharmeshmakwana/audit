"""`faible` command line.

faible verify-log      walk the hash chain; exit 1 at the first broken link
faible rebuild         replay the log into the derived tables; exit 1 unless the derived
                       checksum is identical before and after
faible checksum        print the derived-table checksum
faible seed            load seed data (idempotent)
faible events-doc      regenerate docs/EVENTS.md from faible/events.py
faible show-shot CODE  print a shot's generations, prompts, credits and asset links
faible fingerprint     compute pHash for stored assets that lack one (appends events)
faible fetch-missing   fetch + verify bytes of assets known only by a public URL
faible create-account EMAIL [--role admin]   one Faible account per person (never shared)
faible disable-account EMAIL                 refuse login and every existing token
faible enable-account EMAIL                  allow a disabled account to log in again
faible set-password EMAIL [--password PW]    reset an account's password (prompts if omitted)
faible disable-demo-accounts                 disable the seeded *@studio demo accounts
faible import-history PATH… [--dry-run]      Higgsfield exporter files/folders → the log
                                             (idempotent; attributed to no employee)
faible export-history [--out DIR]            service history + usage captures as exporter
                                             files (generations.json, _raw, usage_raw.json)
"""

import argparse
import sys
from collections.abc import Callable
from functools import partial

from faible.db import get_sessionmaker


def _verify_log() -> int:
    from faible.log import verify_log

    with get_sessionmaker()() as session:
        result = verify_log(session)
    if result.ok:
        print(f"OK: {result.checked} events, hash chain intact")
        return 0
    print(f"BROKEN at seq {result.first_broken_seq}: {result.reason}")
    print(f"({result.checked} events verified before it)")
    return 1


def _checksum() -> int:
    from faible.rebuild import derived_checksum

    with get_sessionmaker()() as session:
        cs = derived_checksum(session)
    for table, (count, digest) in cs.tables.items():
        print(f"  {table:<18} {count:>6} rows  {digest[:16]}")
    print(f"derived checksum: {cs.total}")
    return 0


def _rebuild() -> int:
    from faible.rebuild import derived_checksum, rebuild

    with get_sessionmaker()() as session, session.begin():
        before = derived_checksum(session)
        replayed = rebuild(session)
        after = derived_checksum(session)
        print(f"replayed {replayed} events")
        print(f"before: {before.total}")
        print(f"after:  {after.total}")
        if before.total != after.total:
            for table in before.tables:
                if before.tables[table] != after.tables.get(table):
                    print(f"  differs: {table} {before.tables[table]} → {after.tables.get(table)}")
            print("MISMATCH: rebuild did not reproduce the derived tables; rolled back")
            session.rollback()
            return 1
    print("OK: rebuild reproduced identical derived tables")
    return 0


def _seed() -> int:
    from faible.seed import seed

    with get_sessionmaker()() as session, session.begin():
        results = seed(session)
    print(f"seed: {results['accepted']} events written, {results['duplicate']} already present")
    return 0


def _events_doc() -> int:
    from faible.events_doc import EVENTS_MD, render

    EVENTS_MD.write_text(render(), encoding="utf-8")
    print(f"wrote {EVENTS_MD}")
    return 0


def _show_shot(code: str) -> int:
    from sqlalchemy import select

    from faible.models import Shot
    from faible.views import shot_detail

    with get_sessionmaker()() as session:
        shot = session.scalar(select(Shot).where(Shot.code == code))
        if shot is None:
            print(f"no shot {code}")
            return 1
        d = shot_detail(session, shot.id)
    assert d is not None
    s = d["shot"]
    print(f"{s['project']} / {s['sequence']} / {s['code']}  —  {s['description']}")
    print(f"credits spent: {d['credit_spend']}  (budget {s['credit_budget']})")

    def short(h: str | None) -> str:
        return (h or "?")[:12]

    for i, a in enumerate(d["activities"], 1):
        when = a["started_at"].strftime("%H:%M:%S")
        print(f"\n{i}. {when}  {a['service']} {a['kind']} {a['model_id']}  [{a['status']}]")
        cost = f"{a['credits_cost']} {a['credits_currency']}"
        print(f"   by {a['captured_by']}, {cost}, evidence {a['evidence']}")
        print(f"   prompt: {(a['prompt'] or '(none)')[:200]}")
        for inp in a["inputs"]:
            ref = inp["service_asset_id"] or ""
            state = f"sha256 {short(inp['sha256'])}" if inp["resolved"] else "unresolved"
            print(f"   in  ({inp['role']}) {ref} → {state}")
        for out in a["outputs"]:
            state = f"sha256 {short(out['sha256'])}" if out["resolved"] else "not hashed yet"
            print(f"   out #{out['index']} {out['service_asset_id'] or ''} → {state}")
    for v in d["versions"]:
        head = f"v{v['version_number']} [{v['status']}]"
        print(f"\n{head} sha256 {short(v['sha256'])} by {v['created_by']}")
    return 0


def _backfill(kind: str) -> int:
    from faible.backfill import fetch_missing, fingerprint_stored

    fn = fingerprint_stored if kind == "fingerprint" else fetch_missing
    with get_sessionmaker()() as session, session.begin():
        report = fn(session)
    for d in report.done:
        print(f"  done     {d}")
    for sk in report.skipped:
        print(f"  skipped  {sk}")
    print(f"{kind}: {len(report.done)} done, {len(report.skipped)} skipped")
    return 0


def _setup_test_login() -> int:
    from faible.auth import hash_password, register_user
    from faible.models import UserCredential

    with get_sessionmaker()() as session, session.begin():
        user_id = register_user(
            session,
            email="om@faiblemedia.com",
            password="changeme",  # noqa: S106 - requested test account
            display_name="Om",
        )
        credential = session.get_one(UserCredential, user_id)
        credential.password_hash = hash_password("changeme")
    print("Test login ready: om@faiblemedia.com")
    return 0


def _set_password(email: str, password: str | None) -> int:
    import getpass

    from sqlalchemy import select

    from faible.auth import hash_password
    from faible.models import UserCredential

    password = password or getpass.getpass("New password: ")
    if len(password) < 8:
        print("Password must contain at least 8 characters")
        return 1
    with get_sessionmaker()() as session, session.begin():
        cred = session.scalar(
            select(UserCredential).where(UserCredential.email == email.strip().lower())
        )
        if cred is None:
            print("Account not found")
            return 1
        cred.password_hash = hash_password(password)
    print(f"Password updated: {email.strip().lower()}")
    return 0


def _enable_account(email: str) -> int:
    from faible.auth import enable_account

    with get_sessionmaker()() as session, session.begin():
        if not enable_account(session, email):
            print("Account not found")
            return 1
    print(f"Enabled: {email.strip().lower()}")
    return 0


def _disable_account(email: str) -> int:
    from faible.auth import disable_account

    with get_sessionmaker()() as session, session.begin():
        if not disable_account(session, email):
            print("Account not found")
            return 1
    print(f"Disabled: {email.strip().lower()} (logins and existing tokens are refused)")
    return 0


def _disable_demo_accounts() -> int:
    """The seed's director@studio / artist1@studio / artist2@studio are demo logins with a
    public password. On a real studio install they must not absorb anyone's captures."""
    from sqlalchemy import select

    from faible.auth import disable_account
    from faible.models import UserCredential

    with get_sessionmaker()() as session, session.begin():
        emails = list(
            session.scalars(
                select(UserCredential.email).where(
                    UserCredential.email.like("%@studio"), UserCredential.disabled_at.is_(None)
                )
            )
        )
        for email in emails:
            disable_account(session, email)
    print(f"Disabled {len(emails)} demo account(s): {', '.join(emails) or 'none left'}")
    return 0


def _import_history(paths: list[str], dry_run: bool) -> int:
    from pathlib import Path

    from faible.history_io import import_history

    missing = [p for p in paths if not Path(p).exists()]
    if missing:
        print(f"not found: {', '.join(missing)}")
        return 1
    with get_sessionmaker()() as session:
        reports = import_history(session, [Path(p) for p in paths], dry_run=dry_run)
    totals = {"accepted": 0, "duplicate": 0, "conflict": 0, "rejected": 0}
    for r in reports:
        if r.format == "skipped":
            print(f"  skip   {r.path}: {'; '.join(r.reasons)}")
            continue
        counts = ", ".join(f"{k} {getattr(r, k)}" for k in totals if getattr(r, k))
        extra = f", {r.skipped} without an id" if r.skipped else ""
        extra += f", {r.unchanged} already recorded" if r.unchanged else ""
        print(f"  {r.format:<10} {r.path}: {r.records} records → {counts or 'nothing'}{extra}")
        for reason in r.reasons:
            print(f"             {reason}")
        for k in totals:
            totals[k] += getattr(r, k)
    verb = "would append" if dry_run else "appended"
    print(
        f"import-history: {verb} {totals['accepted']} events, {totals['duplicate']} already "
        f"present, {totals['conflict']} conflicts, {totals['rejected']} rejected"
    )
    return 1 if totals["conflict"] or totals["rejected"] else 0


def _export_history(out: str | None) -> int:
    from datetime import datetime
    from pathlib import Path

    from faible.config import get_settings
    from faible.history_io import export_history

    target = (
        Path(out)
        if out
        else (get_settings().data_dir / "exports" / f"history-{datetime.now():%Y%m%d-%H%M%S}")
    )
    with get_sessionmaker()() as session:
        try:
            written = export_history(session, target)
        except FileExistsError as exc:
            print(exc)
            return 1
    for path in written:
        print(f"  wrote {path}")
    print(f"export-history: {len(written)} files in {target}")
    return 0


COMMANDS: dict[str, Callable[[], int]] = {
    "verify-log": _verify_log,
    "rebuild": _rebuild,
    "checksum": _checksum,
    "seed": _seed,
    "setup-test-login": _setup_test_login,
    "events-doc": _events_doc,
    "fingerprint": partial(_backfill, "fingerprint"),
    "fetch-missing": partial(_backfill, "fetch-missing"),
    "disable-demo-accounts": _disable_demo_accounts,
}


def _create_account(email: str, role: str, password: str | None) -> int:
    import getpass

    from sqlalchemy import select

    from faible.auth import register_user
    from faible.models import User

    email = email.strip().lower()
    if email.count("@") != 1 or email.split("@")[1] != "faiblemedia.com":
        print("Use a company email at faiblemedia.com")
        return 1
    password = password or getpass.getpass("New account password: ")
    if len(password) < 8:
        print("Password must contain at least 8 characters")
        return 1
    with get_sessionmaker()() as session, session.begin():
        existing = session.scalar(select(User).where(User.email == email))
        if existing:
            print(f"Account already exists with role {existing.role}; no changes made")
            return 0 if existing.role == role else 1
        register_user(
            session, email=email, password=password, display_name=email.split("@")[0], role=role
        )
    print(f"Created {role} account: {email}")
    return 0


def _set_account_role(email: str, role: str) -> int:
    import uuid
    from datetime import UTC, datetime

    from sqlalchemy import select

    from faible.log import EventIn, append_events
    from faible.models import User

    with get_sessionmaker()() as session, session.begin():
        user = session.scalar(select(User).where(User.email == email.strip().lower()))
        if user is None:
            print("Account not found")
            return 1
        if user.role == role:
            return 0
        result = append_events(
            session,
            [
                EventIn(
                    event_id=uuid.uuid4(),
                    type="user.role_changed",
                    occurred_at=datetime.now(UTC),
                    evidence="asserted",
                    payload={"user_id": str(user.id), "role": role},
                )
            ],
            source="script",
        )[0]
        if result.status != "accepted":
            print(result.reason)
            return 1
    print(f"Account role set to {role}: {email}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="faible", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "command",
        choices=sorted(
            [
                *COMMANDS,
                "show-shot",
                "create-account",
                "set-account-role",
                "disable-account",
                "enable-account",
                "set-password",
                "import-history",
                "export-history",
            ]
        ),
    )
    parser.add_argument("arg", nargs="?")
    parser.add_argument("paths", nargs="*", help=argparse.SUPPRESS)  # import-history PATH…
    parser.add_argument("--role", choices=["artist", "admin"], default="artist")
    parser.add_argument("--password", help="Account password; omitted to prompt securely")
    parser.add_argument("--dry-run", action="store_true", help="import-history: validate only")
    parser.add_argument("--out", help="export-history: target folder (must be new or empty)")
    args = parser.parse_args(argv)
    if args.command == "import-history":
        if not args.arg:
            parser.error("import-history needs exporter files or folders")
        return _import_history([args.arg, *args.paths], args.dry_run)
    if args.command == "export-history":
        return _export_history(args.out)
    if args.command == "set-account-role":
        if not args.arg:
            parser.error("set-account-role needs an email")
        return _set_account_role(args.arg, args.role)
    if args.command == "set-password":
        if not args.arg:
            parser.error("set-password needs an email")
        return _set_password(args.arg, args.password)
    if args.command == "enable-account":
        if not args.arg:
            parser.error("enable-account needs an email")
        return _enable_account(args.arg)
    if args.command == "disable-account":
        if not args.arg:
            parser.error("disable-account needs an email")
        return _disable_account(args.arg)
    if args.command == "create-account":
        if not args.arg:
            parser.error("create-account needs a company email")
        return _create_account(args.arg, args.role, args.password)
    if args.command == "show-shot":
        if not args.arg:
            parser.error("show-shot needs a shot code, e.g. SQ01_SH010")
        return _show_shot(args.arg)
    return COMMANDS[args.command]()


if __name__ == "__main__":
    sys.exit(main())
