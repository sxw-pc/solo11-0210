"""Run the offline league workbench from a source checkout."""
import argparse
import sqlite3
import sys
from pathlib import Path

from . import eligibility, grounds, ledger, workbench
from .db import connect, encode, require


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "apply", "demo", "court-status", "availability", "round-report", "standings", "audit"):
        cmd = commands.add_parser(name)
        cmd.add_argument("--db", required=True)
        if name == "apply":
            cmd.add_argument("file")
        elif name == "court-status":
            cmd.add_argument("court")
            cmd.add_argument("minute", type=int)
        elif name == "availability":
            cmd.add_argument("person")
            cmd.add_argument("start", type=int)
            cmd.add_argument("stop", type=int)
        elif name == "round-report":
            cmd.add_argument("round")
    opts = parser.parse_args(argv)
    try:
        query = opts.command not in {"init", "apply", "demo"}
        if query:
            require(Path(opts.db).is_file(), "database does not exist")
        with connect(opts.db) as db:
            if query and opts.command != "audit":
                db.execute("BEGIN")
            if opts.command == "init":
                output = {"database": "ready"}
            elif opts.command == "apply":
                output = workbench.apply(db, workbench.load(opts.file))
            elif opts.command == "demo":
                output = {"decisions": workbench.demo(db), **workbench.read_state(db)}
            elif opts.command == "court-status":
                output = grounds.condition(db, opts.court, opts.minute)
            elif opts.command == "availability":
                output = eligibility.availability(db, opts.person, opts.start, opts.stop)
            elif opts.command == "round-report":
                output = workbench.report(db, opts.round)
            elif opts.command == "standings":
                output = ledger.standings(db)
            else:
                output = workbench.audit(db)
        print(encode(output))
        return 1 if opts.command == "audit" and output["drift"] else 0
    except (ValueError, OSError, sqlite3.Error) as exc:
        print(f"rejected: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
