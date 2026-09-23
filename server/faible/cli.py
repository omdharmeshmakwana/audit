"""`faible` command line.

faible verify-log      walk the hash chain; exit 1 at the first broken link
faible rebuild         replay the log into the derived tables; exit 1 unless the derived
                       checksum is identical before and after
faible checksum        print the derived-table checksum
faible seed            load seed data (idempotent)
faible events-doc      regenerate docs/EVENTS.md from faible/events.py
"""

import argparse
import sys

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


COMMANDS = {
    "verify-log": _verify_log,
    "rebuild": _rebuild,
    "checksum": _checksum,
    "seed": _seed,
    "events-doc": _events_doc,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="faible", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("command", choices=sorted(COMMANDS))
    args = parser.parse_args(argv)
    return COMMANDS[args.command]()


if __name__ == "__main__":
    sys.exit(main())
