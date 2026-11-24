"""Round membership, court occupancy, rain offers and accepted moves."""
from . import eligibility, grounds
from .db import entity, integer, one, require


def add_round(db, event, division, earliest, deadline, expected):
    rules = entity(db, "divisions", division)
    integer(earliest, "round earliest")
    require(integer(deadline, "round deadline") - earliest >= rules["duration"], "round too short")
    integer(expected, "expected fixtures", 1)
    db.execute("INSERT INTO rounds VALUES (?,?,?,?,?)", (event, division, earliest, deadline, expected))
    return "round-opened"


def add_fixture(db, event, round, home, away):
    group = entity(db, "rounds", round)
    require(home != away, "fixture requires two different teams")
    for team in (home, away):
        eligibility.participant(db, "team", team, group["division"])
        duplicate = one(db, "SELECT id FROM fixtures WHERE round=? AND (home=? OR away=?)", round, team, team)
        require(duplicate is None, f"team already entered this round: {team}")
    count = one(db, "SELECT COUNT(*) FROM fixtures WHERE round=?", round)[0]
    require(count < group["expected"], "round fixture roster is full")
    db.execute("INSERT INTO fixtures VALUES (?,?,?,?)", (event, round, home, away))
    return "fixture-admitted"


def current(db, fixture):
    return one(db, "SELECT * FROM assignments WHERE fixture=? ORDER BY rev DESC LIMIT 1", fixture)


def latest_result(db, assignment):
    return one(db, "SELECT * FROM results WHERE assignment=? ORDER BY rev DESC LIMIT 1", assignment)


def has_result(db, assignment):
    score = latest_result(db, assignment)
    return score is not None and score["home"] is not None


def participants(match, official):
    return (match["home"], match["away"], official)


def slot_evidence(db, match, court, start, stop, official, known_by=None):
    field = grounds.condition(db, court, start, known_by)
    people = {p: eligibility.availability(db, p, start, stop) for p in participants(match, official)}
    return {"court": field, "people": people}


def slot_blockers(evidence):
    blockers = []
    if evidence["court"]["status"] != "open":
        blockers.append("court-" + evidence["court"]["status"])
    blockers.extend(f"unavailable:{p}" for p, facts in evidence["people"].items() if not facts["available"])
    return blockers


def slot_end(db, match, court, start, official):
    integer(start, "slot start")
    group = entity(db, "rounds", match["round"])
    rules = entity(db, "divisions", group["division"])
    entity(db, "courts", court)
    eligibility.participant(db, "official", official, group["division"])
    stop = start + rules["duration"]
    require(group["earliest"] <= start and stop <= group["deadline"], "slot outside round window")
    return stop


def conflicts(db, match, court, start, stop, official):
    candidates = db.execute("""SELECT a.*, f.home, f.away FROM assignments a JOIN fixtures f ON f.id=a.fixture
         WHERE a.fixture<>? AND a.start<? AND a.stop>?
         AND a.rev=(SELECT MAX(b.rev) FROM assignments b WHERE b.fixture=a.fixture) ORDER BY a.id""",
                            (match["id"], stop, start))
    people = set(participants(match, official))
    for other in candidates:
        require(other["court"] != court, f"court occupied by {other['fixture']}")
        require(not people.intersection(participants(other, other["official"])),
                f"participant occupied by {other['fixture']}")


def write_assignment(db, event, match, rev, court, start, stop, official, promise=None, known_by=None):
    blockers = slot_blockers(slot_evidence(db, match, court, start, stop, official, known_by))
    require(not blockers, "slot blocked: " + ",".join(blockers))
    conflicts(db, match, court, start, stop, official)
    db.execute("INSERT INTO assignments VALUES (?,?,?,?,?,?,?,?)",
               (event, match["id"], rev, court, start, stop, official, promise))


def assign(db, event, fixture, court, start, official):
    match = entity(db, "fixtures", fixture)
    require(current(db, fixture) is None, "use a rain promise for reassignment")
    stop = slot_end(db, match, court, start, official)
    write_assignment(db, event, match, 1, court, start, stop, official)
    return "slot-assigned"


def promise(db, event, fixture, prior, court, start, official, expires):
    match = entity(db, "fixtures", fixture)
    old = current(db, fixture)
    require(old is not None and old["id"] == prior, "stale assignment")
    require(not has_result(db, prior), "recorded result must be voided before movement")
    slot_end(db, match, court, start, official)
    require(start > old["start"], "rain offer must start after the missed slot")
    integer(expires, "promise expiry")
    require(old["start"] <= expires <= start, "expiry outside offer window")
    db.execute("INSERT INTO promises(id,fixture,prior,court,start,official,expires) VALUES (?,?,?,?,?,?,?)",
               (event, fixture, prior, court, start, official, expires))
    return "promise-pending"


def consent(db, event, promise, person, minute):
    offer = entity(db, "promises", promise)
    prior = entity(db, "assignments", offer["prior"])
    require(current(db, offer["fixture"])["id"] == prior["id"], "stale promise revision")
    match = entity(db, "fixtures", offer["fixture"])
    require(person in participants(match, offer["official"]), "consent from unrelated person")
    require(prior["start"] <= integer(minute, "consent minute") <= offer["expires"], "consent outside offer window")
    db.execute("INSERT INTO consents VALUES (?,?,?,?)", (event, promise, person, minute))
    return "consent-recorded"


def move(db, event, promise, minute):
    offer = entity(db, "promises", promise)
    match = entity(db, "fixtures", offer["fixture"])
    old = current(db, match["id"])
    require(not offer["used"] and old["id"] == offer["prior"], "stale or consumed promise")
    require(not has_result(db, old["id"]), "recorded result must be voided before movement")
    require(old["start"] <= integer(minute, "move minute") <= offer["expires"], "promise expired or too early")
    require(grounds.condition(db, old["court"], old["start"])["status"] == "closed", "original slot lacks rain closure")
    signed = {row["person"] for row in db.execute("SELECT person FROM consents WHERE promise=? AND minute<=?",
                                                 (promise, minute))}
    require(signed == set(participants(match, offer["official"])), "missing timely team or official consent")
    stop = slot_end(db, match, offer["court"], offer["start"], offer["official"])
    write_assignment(db, event, match, old["rev"] + 1, offer["court"], offer["start"], stop, offer["official"], promise, known_by=minute)
    db.execute("UPDATE promises SET used=1 WHERE id=?", (promise,))
    return "rain-slot-moved"


def fairness(db, group, assigned):
    rules = entity(db, "divisions", group["division"])
    starts = [row["start"] for row in assigned]
    spread = max(starts) - min(starts) if starts else None
    return {"spread": spread, "limit": rules["spread"],
            "fair": len(starts) == group["expected"] and spread <= rules["spread"]}
