"""Workbench commands, evidence replay and materialized-state comparison."""
import json
from pathlib import Path

from . import eligibility, grounds, ledger, schedule
from .db import connect, ingest, require


FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
ACTIONS = {
    "court": grounds.add_court, "inspect": grounds.inspect,
    "division": eligibility.division, "team": eligibility.team, "official": eligibility.official,
    "window": eligibility.window, "round": schedule.add_round, "fixture": schedule.add_fixture,
    "assign": schedule.assign, "promise": schedule.promise, "consent": schedule.consent,
    "move": schedule.move, "result": ledger.result, "settle": ledger.settle, "recall": ledger.recall,
}


def load(path):
    records = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    require(isinstance(records, list), "fixture must be an array of events")
    return records


def apply(db, records):
    return ingest(db, records, ACTIONS)


def demo(db):
    return apply(db, load(FIXTURES / "league.json") + load(FIXTURES / "late-recall.json"))


def report(db, group):
    live = ledger.round_report(db, group)
    published = ledger.active(db, group)
    live["settlement"] = published["id"] if published else None
    live["release"] = "unsettled"
    if published:
        draft = {key: value for key, value in live.items() if key not in ("settlement", "release")}
        live["release"] = "current" if json.loads(published["evidence"]) == draft else "review-required"
    return live


def read_state(db):
    return {"rounds": [report(db, row[0]) for row in db.execute("SELECT id FROM rounds ORDER BY id")],
            "standings": ledger.standings(db),
            "settlements": [dict(row) for row in db.execute("SELECT id,round,reverses FROM settlements ORDER BY id")]}


def audit(db):
    db.execute("BEGIN")
    try:
        rows = db.execute("SELECT id,body FROM events ORDER BY seq").fetchall()
        records = [json.loads(row["body"]) for row in rows]
        require(all(row["id"] == payload.get("id") for row, payload in zip(rows, records)), "journal identity mismatch")
        with connect(":memory:") as replayed:
            apply(replayed, records)
            tables = [row[0] for row in replayed.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
            drift = []
            for table in tables:
                actual = [tuple(row) for row in db.execute(f'SELECT * FROM "{table}" ORDER BY rowid')]
                rebuilt = [tuple(row) for row in replayed.execute(f'SELECT * FROM "{table}" ORDER BY rowid')]
                if actual != rebuilt:
                    drift.append(table)
            return {"audit": "verified" if not drift else "projection-drift", "events": len(records),
                    "drift": drift, **read_state(replayed)}
    finally:
        db.rollback()
