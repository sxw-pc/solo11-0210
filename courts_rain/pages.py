"""Raw score-sheet pages, copy groups, duty confirmations and score reconstruction.

A page records, for one occupancy revision (assignment) and one physical original
(origin), the home/away deltas observed inside the match-minute interval
(start, stop]; minute 0 is the slot start and both teams begin at 0.  Pages may
arrive late, be retransmitted or overlap.  Copies transcribed from one original
share (assignment, origin, start, stop); differing delta claims inside such a
group need a duty confirmation that names the complete conflict group it saw.
Reconstruction treats cumulative scores as non-decreasing integer functions and
solves the pages as difference constraints over interval endpoints, so the work
depends on the number of pages, never on the score values.
"""
from .db import encode, entity, identity, integer, require


def _match(db, fixture, assignment):
    match = entity(db, "fixtures", fixture)
    slot = entity(db, "assignments", assignment)
    require(slot["fixture"] == match["id"], "assignment does not belong to fixture")
    return match


def _duration(db, match):
    group = entity(db, "rounds", match["round"])
    return entity(db, "divisions", group["division"])["duration"]


def add_page(db, event, fixture, assignment, origin, start, stop, home, away):
    match = _match(db, fixture, assignment)
    identity(origin)
    integer(start, "page start")
    require(integer(stop, "page stop") > start and stop <= _duration(db, match),
            "page interval outside match")
    integer(home, "page home delta")
    integer(away, "page away delta")
    db.execute("INSERT INTO pages VALUES (?,?,?,?,?,?,?,?)",
               (event, fixture, assignment, origin, start, stop, home, away))
    return "page-recorded"


def _claims_of(rows):
    return sorted({(row["home"], row["away"]) for row in rows})


def _group(db, assignment, origin, start, stop):
    return db.execute("""SELECT * FROM pages WHERE assignment=? AND origin=? AND start=? AND stop=?
                      ORDER BY rowid""", (assignment, origin, start, stop)).fetchall()


def confirm(db, event, fixture, assignment, origin, start, stop, home, away, claims):
    """Bind the complete conflict group as seen and pick one copy's deltas."""
    _match(db, fixture, assignment)
    identity(origin)
    integer(start, "page start")
    integer(stop, "page stop")
    integer(home, "confirmed home delta")
    integer(away, "confirmed away delta")
    require(isinstance(claims, list)
            and all(isinstance(pair, list) and len(pair) == 2 for pair in claims), "invalid claims")
    seen = sorted({(integer(pair[0], "claim home"), integer(pair[1], "claim away")) for pair in claims})
    current = _claims_of(_group(db, assignment, origin, start, stop))
    require(len(current) >= 2, "no conflicting copies to confirm")
    require(seen == current, "confirmation must bind the complete conflict group")
    require((home, away) in current, "confirmed copy not in conflict group")
    db.execute("INSERT INTO confirmations VALUES (?,?,?,?,?,?,?,?,?)",
               (event, fixture, assignment, origin, start, stop, home, away,
                encode([list(pair) for pair in seen])))
    return "copy-confirmed"


def _effective(db, assignment):
    """One claim per copy group; retransmissions keep a confirmation, new claims void it."""
    rows = db.execute("SELECT * FROM pages WHERE assignment=? ORDER BY rowid", (assignment,)).fetchall()
    groups = {}
    for row in rows:
        groups.setdefault((row["origin"], row["start"], row["stop"]), []).append(row)
    effective, unresolved = [], []
    for origin, start, stop in sorted(groups):
        members = groups[(origin, start, stop)]
        claims = _claims_of(members)
        entry = {"start": start, "stop": stop, "pages": sorted(row["id"] for row in members)}
        if len(claims) == 1:
            effective.append({**entry, "home": claims[0][0], "away": claims[0][1], "source": "unanimous"})
            continue
        stamped = encode([list(pair) for pair in claims])
        valid = None
        for conf in db.execute("""SELECT * FROM confirmations
                                  WHERE assignment=? AND origin=? AND start=? AND stop=? ORDER BY rowid""",
                               (assignment, origin, start, stop)):
            if conf["claims"] == stamped:
                valid = conf
        if valid is None:
            unresolved.append({**entry, "claims": [list(pair) for pair in claims]})
        else:
            effective.append({**entry, "home": valid["home"], "away": valid["away"], "source": valid["id"]})
    return effective, unresolved


def _longest(count, edges):
    """Longest distances from node 0, or None when a positive cycle contradicts the bounds."""
    dist = [None] * count
    dist[0] = 0
    for _ in range(count - 1):
        improved = False
        for u, v, w in edges:
            if dist[u] is not None and (dist[v] is None or dist[v] < dist[u] + w):
                dist[v] = dist[u] + w
                improved = True
        if not improved:
            return dist
    if any(dist[u] is not None and (dist[v] is None or dist[v] < dist[u] + w) for u, v, w in edges):
        return None
    return dist


def _side(endpoints, deltas, extra=()):
    """Lower/upper cumulative bounds per endpoint for one team; None if inconsistent."""
    pos = {minute: index for index, minute in enumerate(endpoints)}
    edges = [(at, at + 1, 0) for at in range(len(endpoints) - 1)]
    for start, stop, delta in deltas:
        edges.append((pos[start], pos[stop], delta))
        edges.append((pos[stop], pos[start], -delta))
    edges.extend(extra)
    lower = _longest(len(endpoints), edges)
    if lower is None:
        return None
    reach = _longest(len(endpoints), [(v, u, w) for u, v, w in edges])
    upper = [None if bound is None else -bound for bound in reach]
    return lower, upper


def _gaps(effective, duration):
    gaps, cursor = [], 0
    for start, stop in sorted({(page["start"], page["stop"]) for page in effective}):
        if start > cursor:
            gaps.append([cursor, start])
        cursor = max(cursor, stop)
    if cursor < duration:
        gaps.append([cursor, duration])
    return gaps


def _feasible(endpoints, pages):
    return all(_side(endpoints, [(page["start"], page["stop"], page[side]) for page in pages]) is not None
               for side in ("home", "away"))


def _unsat_core(endpoints, pages):
    """A minimal set of effective pages whose interval claims cannot coexist."""
    core = list(pages)
    for page in list(core):
        trial = [other for other in core if other is not page]
        if not _feasible(endpoints, trial):
            core = trial
    return sorted(page_id for page in core for page_id in page["pages"])


def analyze(db, assignment):
    """Judge whether the pages on one occupancy revision determine a complete score."""
    slot = entity(db, "assignments", assignment)
    match = entity(db, "fixtures", slot["fixture"])
    duration = _duration(db, match)
    effective, unresolved = _effective(db, assignment)
    report = {"assignment": assignment, "fixture": slot["fixture"], "duration": duration,
              "effective": effective}
    if unresolved:
        return {**report, "status": "contradictory", "reason": "unconfirmed-copies",
                "conflict": unresolved[0]["pages"], "claims": unresolved[0]["claims"]}
    endpoints = sorted({0, duration} | {page["start"] for page in effective}
                       | {page["stop"] for page in effective})
    sides = {}
    for side in ("home", "away"):
        solved = _side(endpoints, [(page["start"], page["stop"], page[side]) for page in effective])
        if solved is None:
            return {**report, "status": "contradictory", "reason": "inconsistent",
                    "conflict": _unsat_core(endpoints, effective)}
        sides[side] = solved
    final = endpoints.index(duration)
    gaps = _gaps(effective, duration)
    lower = {side: sides[side][0] for side in ("home", "away")}
    upper = {side: sides[side][1] for side in ("home", "away")}
    bounds = {side: [lower[side][final], upper[side][final]] for side in ("home", "away")}
    if bounds["home"][0] == bounds["home"][1] and bounds["away"][0] == bounds["away"][1]:
        return {**report, "status": "unique", "gaps": gaps, "bounds": bounds, "open": False,
                "score": {"home": lower["home"][final], "away": lower["away"][final]}}
    raised_side = "home" if upper["home"][final] != lower["home"][final] else "away"
    raised = _side(endpoints, [(page["start"], page["stop"], page[raised_side]) for page in effective],
                   [(0, final, lower[raised_side][final] + 1)])
    require(raised is not None, "witness construction failed")
    second = dict(lower)
    second[raised_side] = raised[0]

    def witness(values):
        return [[minute, values["home"][at], values["away"][at]] for at, minute in enumerate(endpoints)]

    return {**report, "status": "ambiguous", "gaps": gaps, "bounds": bounds,
            "open": upper["home"][final] is None or upper["away"][final] is None,
            "witnesses": [witness(lower), witness(second)]}
