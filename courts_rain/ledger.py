"""Result validity, simultaneous round release and compensating league entries."""
from collections import defaultdict

from . import grounds, pages, schedule
from .db import encode, entity, identity, integer, one, require


def active(db, group):
    return one(db, """SELECT * FROM settlements s WHERE round=? AND reverses IS NULL
                  AND NOT EXISTS (SELECT 1 FROM settlements r WHERE r.reverses=s.id)""", group)


def result(db, event, fixture, assignment, revision, minute, home, away):
    match = entity(db, "fixtures", fixture)
    slot = schedule.current(db, fixture)
    require(slot is not None and slot["id"] == assignment, "result targets stale assignment")
    require(active(db, match["round"]) is None, "recall the active settlement before changing a result")
    require(integer(minute, "result minute") >= slot["stop"], "match has not ended")
    previous = schedule.latest_result(db, assignment)
    expected = previous["rev"] + 1 if previous else 1
    require(previous is None or minute >= previous["minute"], "result revision predates prior record")
    require(integer(revision, "result revision", 1) == expected, "stale result revision")
    if home is None and away is None:
        require(previous is not None and previous["home"] is not None, "no score to void")
    else:
        integer(home, "home score")
        integer(away, "away score")
        blockers = schedule.slot_blockers(schedule.slot_evidence(db, match, slot["court"], slot["start"], slot["stop"], slot["official"]))
        require(not blockers, "result ineligible: " + ",".join(blockers))
    db.execute("INSERT INTO results VALUES (?,?,?,?,?,?)", (event, assignment, revision, minute, home, away))
    return "result-voided" if home is None else "result-admitted"


def tally(db, event, fixture, assignment, revision, minute):
    """Turn a uniquely determined page set into an official result under the ledger rules."""
    entity(db, "fixtures", fixture)
    slot = schedule.current(db, fixture)
    require(slot is not None and slot["id"] == assignment, "tally targets stale assignment")
    report = pages.analyze(db, assignment)
    detail = report["status"]
    if report["status"] == "contradictory":
        detail += ": " + ",".join(report["conflict"])
    require(report["status"] == "unique", "score pages " + detail)
    score = report["score"]
    return result(db, event, fixture, assignment, revision, minute, score["home"], score["away"])


def round_report(db, group_id):
    group = entity(db, "rounds", group_id)
    rows = db.execute("SELECT * FROM fixtures WHERE round=? ORDER BY id", (group_id,)).fetchall()
    matches, assigned, blockers = [], [], []
    for match in rows:
        fixture = match["id"]
        slot = schedule.current(db, fixture)
        if slot is None:
            matches.append({"fixture": fixture, "assignment": None})
            blockers.append(f"unassigned:{fixture}")
            continue
        assigned.append(slot)
        fields = schedule.slot_evidence(db, match, slot["court"], slot["start"], slot["stop"], slot["official"])
        reasons = schedule.slot_blockers(fields)
        score = schedule.latest_result(db, slot["id"])
        if score is None or score["home"] is None:
            reasons.append("missing-result")
        ancestor = slot
        closures = []
        while ancestor["promise"]:
            offer = entity(db, "promises", ancestor["promise"])
            ancestor = entity(db, "assignments", offer["prior"])
            closure = grounds.condition(db, ancestor["court"], ancestor["start"])
            closures.append({"assignment": ancestor["id"], "promise": offer["id"], **closure})
            if closure["status"] != "closed":
                reasons.append(f"rain-closure-disputed:{ancestor['id']}")
        fields["rain_history"] = closures
        blockers.extend(f"{fixture}:{reason}" for reason in reasons)
        matches.append({"fixture": fixture, "assignment": slot["id"], "promise": slot["promise"],
                        "start": slot["start"], "result": dict(score) if score else None, "evidence": fields})
    fairness = schedule.fairness(db, group, assigned)
    if len(rows) != group["expected"]:
        blockers.append("incomplete-roster")
    if not fairness["fair"]:
        blockers.append("round-spread-or-coverage")
    return {"round": group_id, "division": group["division"], "rules": dict(entity(db, "divisions", group["division"])),
            "fairness": fairness, "matches": matches, "blockers": blockers}


def settle(db, event, round):
    require(active(db, round) is None, "round already settled")
    report = round_report(db, round)
    require(not report["blockers"], "round held: " + ",".join(report["blockers"]))
    db.execute("INSERT INTO settlements VALUES (?,?,NULL,?)", (event, round, encode(report)))
    rules = report["rules"]
    for played in report["matches"]:
        match = entity(db, "fixtures", played["fixture"])
        score = played["result"]
        for side, other in (("home", "away"), ("away", "home")):
            margin = score[side] - score[other]
            outcome = "win" if margin > 0 else "loss" if margin < 0 else "draw"
            db.execute("INSERT INTO entries VALUES (?,?,1,?,?)", (event, match[side], rules[outcome], margin))
    return "round-settled"


def recall(db, event, round, expected, reason):
    original = active(db, round)
    require(original is not None and original["id"] == expected, "stale or absent settlement")
    identity(reason)
    report = round_report(db, round)
    db.execute("INSERT INTO settlements VALUES (?,?,?,?)", (event, round, expected, encode({"reason": reason, **report})))
    db.execute("""INSERT INTO entries SELECT ?,team,-played,-points,-difference FROM entries WHERE settlement=?""",
               (event, expected))
    return "settlement-recalled"


def standings(db):
    groups = defaultdict(list)
    for row in db.execute("""SELECT t.division,t.id AS team,COALESCE(SUM(e.played),0) AS played,
       COALESCE(SUM(e.points),0) AS points,COALESCE(SUM(e.difference),0) AS difference
       FROM teams t LEFT JOIN entries e ON t.id=e.team GROUP BY t.id
       ORDER BY t.division,points DESC,difference DESC,t.id"""):
        item = dict(row)
        groups[item.pop("division")].append(item)
    for rows in groups.values():
        last = None
        for index, row in enumerate(rows, 1):
            key = (row["points"], row["difference"])
            rank = index if key != last else rank
            row["rank"] = rank
            last = key
    return dict(groups)
