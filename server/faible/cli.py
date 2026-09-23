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


COMMANDS: dict[str, Callable[[], int]] = {
    "verify-log": _verify_log,
    "rebuild": _rebuild,
    "checksum": _checksum,
    "seed": _seed,
    "events-doc": _events_doc,
    "fingerprint": partial(_backfill, "fingerprint"),
    "fetch-missing": partial(_backfill, "fetch-missing"),
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="faible", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("command", choices=sorted([*COMMANDS, "show-shot"]))
    parser.add_argument("arg", nargs="?")
    args = parser.parse_args(argv)
    if args.command == "show-shot":
        if not args.arg:
            parser.error("show-shot needs a shot code, e.g. SQ01_SH010")
        return _show_shot(args.arg)
    return COMMANDS[args.command]()


if __name__ == "__main__":
    sys.exit(main())
