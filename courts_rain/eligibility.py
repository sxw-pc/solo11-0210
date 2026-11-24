"""Division eligibility and explicit team or official availability windows."""
from .db import entity, identity, integer, require


def division(db, event, win, draw, loss, duration, spread):
    for value in (win, draw, loss, spread):
        integer(value, "division rule")
    integer(duration, "match duration", 1)
    require(win > draw >= loss, "points must follow win > draw >= loss")
    db.execute("INSERT INTO divisions VALUES (?,?,?,?,?,?)", (event, win, draw, loss, duration, spread))
    return "scoring-rules-frozen"


def team(db, event, division):
    entity(db, "divisions", division)
    require(db.execute("SELECT 1 FROM officials WHERE id=?", (event,)).fetchone() is None, "identity in use")
    db.execute("INSERT INTO teams VALUES (?,?)", (event, division))
    return "team-enrolled"


def official(db, event, division):
    entity(db, "divisions", division)
    require(db.execute("SELECT 1 FROM teams WHERE id=?", (event,)).fetchone() is None, "identity in use")
    db.execute("INSERT INTO officials VALUES (?,?)", (event, division))
    return "official-qualified"


def participant(db, kind, person, division):
    table = {"team": "teams", "official": "officials"}.get(kind)
    require(table is not None, "unknown participant kind")
    row = entity(db, table, person)
    require(row["division"] == division, f"out-of-division {kind}: {person}")
    return row


def window(db, event, kind, person, start, stop, available):
    identity(kind)
    table = {"team": "teams", "official": "officials"}.get(kind)
    require(table is not None, "unknown participant kind")
    entity(db, table, person)
    integer(start, "window start")
    require(integer(stop, "window end") > start and type(available) is bool, "invalid availability window")
    db.execute("INSERT INTO windows VALUES (?,?,?,?,?,?)", (event, person, kind, start, stop, available))
    return "availability-recorded" if available else "blackout-recorded"


def availability(db, person, start, stop):
    identity(person)
    require(db.execute("SELECT id FROM teams WHERE id=? UNION SELECT id FROM officials WHERE id=?",
                       (person, person)).fetchone() is not None, f"unknown participant: {person}")
    integer(start, "availability start")
    require(integer(stop, "availability end") > start, "empty availability interval")
    evidence = db.execute("""SELECT * FROM windows WHERE person=? AND start<? AND stop>?
                             ORDER BY start,stop,id""", (person, stop, start)).fetchall()
    end = start
    for row in evidence:
        if row["available"] and row["start"] <= end:
            end = max(end, row["stop"])
    return {"available": end >= stop and all(row["available"] for row in evidence),
            "windows": [row["id"] for row in evidence]}
