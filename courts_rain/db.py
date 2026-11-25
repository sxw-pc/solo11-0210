"""SQLite projection schema and a transactional offline event journal."""
import json
import sqlite3
from contextlib import contextmanager


SCHEMA = """
CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY, id TEXT UNIQUE, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS device_journal(
 machine TEXT NOT NULL, seq INTEGER NOT NULL, id TEXT NOT NULL UNIQUE,
 PRIMARY KEY(machine, seq),
 FOREIGN KEY(id) REFERENCES events(id));
CREATE TABLE IF NOT EXISTS courts(id TEXT PRIMARY KEY, surface TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS inspections(
 id TEXT PRIMARY KEY, court TEXT NOT NULL REFERENCES courts, minute INTEGER NOT NULL,
 wet INTEGER NOT NULL, debris INTEGER NOT NULL, UNIQUE(court, minute));
CREATE TABLE IF NOT EXISTS divisions(
 id TEXT PRIMARY KEY, win INTEGER NOT NULL, draw INTEGER NOT NULL,
 loss INTEGER NOT NULL, duration INTEGER NOT NULL, spread INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS teams(id TEXT PRIMARY KEY, division TEXT NOT NULL REFERENCES divisions);
CREATE TABLE IF NOT EXISTS officials(id TEXT PRIMARY KEY, division TEXT NOT NULL REFERENCES divisions);
CREATE TABLE IF NOT EXISTS windows(
 id TEXT PRIMARY KEY, person TEXT NOT NULL, kind TEXT NOT NULL, start INTEGER NOT NULL,
 stop INTEGER NOT NULL, available INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS rounds(
 id TEXT PRIMARY KEY, division TEXT NOT NULL REFERENCES divisions,
 earliest INTEGER NOT NULL, deadline INTEGER NOT NULL, expected INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS fixtures(
 id TEXT PRIMARY KEY, round TEXT NOT NULL REFERENCES rounds,
 home TEXT NOT NULL REFERENCES teams, away TEXT NOT NULL REFERENCES teams);
CREATE TABLE IF NOT EXISTS assignments(
 id TEXT PRIMARY KEY, fixture TEXT NOT NULL REFERENCES fixtures, rev INTEGER NOT NULL,
 court TEXT NOT NULL REFERENCES courts, start INTEGER NOT NULL, stop INTEGER NOT NULL,
 official TEXT NOT NULL REFERENCES officials, promise TEXT REFERENCES promises,
 UNIQUE(fixture,rev));
CREATE TABLE IF NOT EXISTS promises(
 id TEXT PRIMARY KEY, fixture TEXT NOT NULL REFERENCES fixtures, prior TEXT NOT NULL REFERENCES assignments,
 court TEXT NOT NULL REFERENCES courts, start INTEGER NOT NULL, official TEXT NOT NULL REFERENCES officials,
 expires INTEGER NOT NULL, used INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS consents(
 id TEXT PRIMARY KEY, promise TEXT NOT NULL REFERENCES promises, person TEXT NOT NULL,
 minute INTEGER NOT NULL, UNIQUE(promise,person));
CREATE TABLE IF NOT EXISTS results(
 id TEXT PRIMARY KEY, assignment TEXT NOT NULL REFERENCES assignments,
 rev INTEGER NOT NULL, minute INTEGER NOT NULL, home INTEGER, away INTEGER, UNIQUE(assignment,rev));
CREATE TABLE IF NOT EXISTS settlements(
 id TEXT PRIMARY KEY, round TEXT NOT NULL REFERENCES rounds, reverses TEXT UNIQUE REFERENCES settlements,
 evidence TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS entries(
 settlement TEXT NOT NULL REFERENCES settlements, team TEXT NOT NULL REFERENCES teams,
 played INTEGER NOT NULL, points INTEGER NOT NULL, difference INTEGER NOT NULL,
 PRIMARY KEY(settlement, team));
"""


class Rejected(ValueError):
    """A business rule prevented a state change."""


def require(condition, message):
    if not condition:
        raise Rejected(message)


def integer(value, label, minimum=0):
    require(type(value) is int and value >= minimum, f"invalid {label}")
    return value


def identity(value):
    require(isinstance(value, str) and bool(value.strip()) and value == value.strip(), "invalid ID")
    return value


def encode(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)


@contextmanager
def connect(path):
    db = sqlite3.connect(path, isolation_level=None)
    try:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.executescript(SCHEMA)
        yield db
    finally:
        db.close()


def one(db, sql, *args):
    return db.execute(sql, args).fetchone()


def backup(source, target):
    """Copy every committed row of ``source`` into another open connection."""
    source.backup(target)
    return target


def entity(db, table, key):
    identity(key)
    row = one(db, f"SELECT * FROM {table} WHERE id=?", key)
    require(row is not None, f"unknown {table}: {key}")
    return row


def judge(db, record, actions, provenance=None):
    """Apply one event's existing business adjudication.

    Identical retransmissions are safe. When ``provenance`` (machine, raw
    sequence) is supplied, a known sequence must always reference the same
    event: same number, different content is an explicit conflict.
    """
    require(isinstance(record, dict), "event must be an object")
    event = identity(record.get("id"))
    action = record.get("op")
    require(isinstance(action, str) and action in actions, "unknown operation")
    body = encode(record)
    if provenance is not None:
        machine, raw_seq = provenance
        logged = one(db, "SELECT id FROM device_journal WHERE machine=? AND seq=?", machine, raw_seq)
        require(logged is None or logged["id"] == event,
                f"device sequence conflict: {machine}#{raw_seq}")
    old = one(db, "SELECT body FROM events WHERE id=?", event)
    if old:
        require(old["body"] == body, f"event ID conflict: {event}")
        return {"id": event, "decision": "duplicate"}
    args = {k: v for k, v in record.items() if k not in ("id", "op")}
    try:
        decision = actions[action](db, event, **args)
    except TypeError as exc:
        raise Rejected(f"invalid arguments for {action}: {exc}") from exc
    db.execute("INSERT INTO events(id,body) VALUES (?,?)", (event, body))
    if provenance is not None:
        db.execute("INSERT INTO device_journal(machine,seq,id) VALUES (?,?,?)", (machine, raw_seq, event))
    return {"id": event, "decision": decision}


def ingest(db, records, actions, provenance=None):
    """A file is all-or-nothing. Retried event IDs must have identical content."""
    decisions = []
    db.execute("BEGIN IMMEDIATE")
    try:
        for index, record in enumerate(records):
            origin = (provenance[0], provenance[1] + index) if provenance is not None else None
            decisions.append(judge(db, record, actions, origin))
        db.commit()
    except BaseException:
        db.rollback()
        raise
    return decisions
