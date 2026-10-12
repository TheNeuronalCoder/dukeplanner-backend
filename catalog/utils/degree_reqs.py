"""B.A./B.S. degree requirements (philosophy, foreign language, quantitative,
scientific literacy) evaluated against taken courses.

The requirements live in data/degree_requirements.yaml. Each matches courses by
subject, optionally limited to listed numbers or a minimum number. A course used
for General Education can't also count, and each course counts toward at most
one degree requirement.
"""
import re
from functools import lru_cache
from pathlib import Path

import yaml

from .courses import course_credits, course_number
from .status import SATISFIED, UNSATISFIED

DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "degree_requirements.yaml"


@lru_cache(maxsize=1)
def load():
    with open(DATA_PATH) as f:
        data = yaml.safe_load(f)
    for req in data["requirements"]:
        req["_pattern"] = re.compile(req["placeholder_pattern"], re.IGNORECASE)
    return data


def requirement_id_for(text):
    """The id of the degree requirement a placeholder stands for, if any."""
    for req in load()["requirements"]:
        if req["_pattern"].search(text or ""):
            return req["id"]
    return None


def _matches(req, course):
    if course.code in req["exclude"]:
        return False
    for m in req["match"]:
        if m["subject"] != course.prefix_id:
            continue
        if "numbers" in m and re.match(r"\d+", course.number).group() not in m["numbers"]:
            continue
        if "number_min" in m and (course_number(course) or 0) < m["number_min"]:
            continue
        return True
    return False


def _met(req, courses):
    if "min_credits" in req and sum(map(course_credits, courses)) < req["min_credits"]:
        return False
    return len(courses) >= req.get("min_count", 0)


def evaluate(ids, taken, gened_used):
    """Evaluate the degree requirements with the given ids.

    `taken` is a dict of pk -> Course and `gened_used` the courses counted toward
    General Education. Returns {id: (json, courses_used)}.
    """
    reqs = [r for r in load()["requirements"] if r["id"] in ids]
    gened_pks = {c.pk for c in gened_used}
    available = [c for c in taken.values() if c.pk not in gened_pks]
    candidates = {r["id"]: [c for c in available if _matches(r, c)] for r in reqs}

    # Fill the requirements with the fewest options first, preferring courses
    # that fewer other requirements could use.
    def demand(course):
        return sum(course in cands for cands in candidates.values())

    used_pks = set()
    results = {}
    for req in sorted(reqs, key=lambda r: len(candidates[r["id"]])):
        chosen = []
        for course in sorted(candidates[req["id"]], key=lambda c: (demand(c), c.code)):
            if _met(req, chosen):
                break
            if course.pk not in used_pks:
                chosen.append(course)
                used_pks.add(course.pk)
        results[req["id"]] = (
            {
                "id": req["id"],
                "name": req["name"],
                "degree": req["degree"],
                "min_credits": req.get("min_credits"),
                "min_count": req.get("min_count"),
                "notes": req["notes"],
                "status": SATISFIED if _met(req, chosen) else UNSATISFIED,
                "credits_earned": sum(map(course_credits, chosen)),
                "satisfied_by": sorted(c.code for c in chosen),
            },
            chosen,
        )
    return {r["id"]: results[r["id"]] for r in reqs}
