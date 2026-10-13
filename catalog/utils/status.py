SATISFIED = 'satisfied'
UNSATISFIED = 'unsatisfied'
# Placeholder groups (e.g. "Philosophy course (in addition to General Education)")
# can't be checked against a course list, so anything depending on them is
# reported separately. General Education placeholders are checked against
# data/gened.json instead (see gened.py), B.A./B.S. requirements against
# data/degree_requirements.json (see degree_reqs.py), and University electives
# against the taken courses no other requirement uses.
UNVERIFIED = 'unverified'


def combine(statuses):
    if UNSATISFIED in statuses:
        return UNSATISFIED
    if UNVERIFIED in statuses:
        return UNVERIFIED
    return SATISFIED
