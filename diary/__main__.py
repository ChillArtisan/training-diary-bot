"""CLI: python -m diary [--db PATH] import|today|cycle|suggestions"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime

from . import program
from .db import Db
from .ingest import MSK, import_export
from .today import build_plan, render_text


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="diary")
    ap.add_argument("--db", default=os.environ.get("DIARY_DB", "data/diary.db"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    imp = sub.add_parser("import", help="import a Telegram export (folder, result.json or messages.html)")
    imp.add_argument("path")
    imp.add_argument("--no-activate", action="store_true", help="do not activate the latest program")
    imp.add_argument("--show-unparsed", action="store_true")
    td = sub.add_parser("today", help="print the next training day")
    td.add_argument("--date", type=date.fromisoformat, default=None)
    sub.add_parser("cycle", help="print the active program and the next day")
    sub.add_parser("suggestions", help="print open exercise merge suggestions")
    args = ap.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    db = Db(args.db)

    if args.cmd == "import":
        r = import_export(db, args.path, activate_latest=not args.no_activate)
        c = db.counts()
        print(f"posts: {r.posts} {r.kinds}")
        print(f"sessions: {c['sessions']}, sets: {c['sets']}, exercises: {c['exercises']}")
        print(f"exercise lines parsed: {r.lines}, unparsed data lines: {len(r.unparsed)} ({r.parse_share:.1%})")
        print(f"new exercises: {r.new_exercises}, merge suggestions: {r.suggestions}")
        if r.activated:
            print(f"activated program #{r.activated}")
        if args.show_unparsed:
            for mid, line in r.unparsed:
                print(f"  #{mid}: {line}")
    elif args.cmd == "today":
        plan = build_plan(db, args.date or datetime.now(MSK).date())
        print(render_text(plan) if plan else "no active program")
    elif args.cmd == "cycle":
        prog = db.active_program()
        if not prog:
            print("no active program")
            return 1
        nxt = program.next_day(db)
        print(f"program #{prog['id']} from {prog['day']}: {prog['name']}")
        for d in db.program_days(prog["id"]):
            mark = "→" if nxt and d["id"] == nxt["id"] else " "
            print(f"{mark} {d['ord'] + 1:>2}. неделя {d['week']} · {d['title']}")
    elif args.cmd == "suggestions":
        for s in db.open_suggestions():
            print(f"#{s['id']} {s['name']!r} ~ {s['candidate']!r} ({s['score']:.2f})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
