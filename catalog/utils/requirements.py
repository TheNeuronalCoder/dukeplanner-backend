"""Evaluation of a program's requirement groups against taken courses."""
import re
from collections import defaultdict

from django.db.models import Prefetch

from ..models import (
    CourseFilter,
    CourseRequirement,
    ProgramRequirement,
    RequirementGroup,
)
from . import degree_reqs
from . import gened as gen_ed
from .courses import course_credits, course_json, course_number
from .status import SATISFIED, UNSATISFIED, UNVERIFIED, combine

# "Electives", "University electives", "University Elective(s)", ...
ELECTIVES_RE = re.compile(r'\s*(university\s+)?electives?(\(s\))?\s*', re.IGNORECASE)

DEGREE_REQUIREMENTS = 'Degree Requirements'
# Placeholders in "Degree Requirements" that summarize the program's other
# sections, e.g. "Major requirements", "B.B.A. core courses", "Cognate
# requirements", "Music Education, Vocal Track Requirements".
SECTION_REFERENCE_RE = re.compile(
    r'\b(major|core|concentration|cognate|prerequisites?|foundational|tracks?|'
    r'sequence|licensure|biology requirements|nursing courses)\b',
    re.IGNORECASE,
)
# Credit grants (e.g. "Credit for Associate Degree and prerequisites") aren't
# section references even if they mention one.
NOT_SECTION_REFERENCE_RE = re.compile(r'^\s*(credit|experiential)\b', re.IGNORECASE)
# Sections that are informational rather than requirements.
INFORMATIONAL_SECTION_RE = re.compile(r'substitution|sample curriculum', re.IGNORECASE)

Kind = RequirementGroup.Kind
EachUnit = RequirementGroup.EachUnit


def _filter_matches(f, course):
    if f.course_id is not None:
        return f.course_id == course.pk
    if f.subject_id is not None and f.subject_id != course.prefix_id:
        return False
    if f.number_min is not None or f.number_max is not None:
        number = course_number(course)
        if number is None:
            return False
        if f.number_min is not None and number < f.number_min:
            return False
        if f.number_max is not None and number > f.number_max:
            return False
    return True


def _filtered_courses(filters, taken):
    """Taken courses matching at least one include filter and no exclude filter."""
    includes = [f for f in filters if not f.exclude]
    excludes = [f for f in filters if f.exclude]
    if not includes:
        return []
    return [
        c
        for c in taken
        if any(_filter_matches(f, c) for f in includes)
        and not any(_filter_matches(f, c) for f in excludes)
    ]


def _is_university_electives(group):
    """The "University electives" slot of a program's degree requirements."""
    return (
        group.kind == Kind.PLACEHOLDER
        and ELECTIVES_RE.fullmatch(group.text or '') is not None
        and group.program_requirement.title == DEGREE_REQUIREMENTS
    )


def _degree_requirement_id(group):
    """The B.A./B.S. requirement (e.g. "quantitative") a placeholder stands for."""
    if (
        group.kind != Kind.PLACEHOLDER
        or group.program_requirement.title != DEGREE_REQUIREMENTS
    ):
        return None
    return degree_reqs.requirement_id_for(group.text)


def _is_section_reference(group):
    text = group.text or ''
    return (
        group.kind == Kind.PLACEHOLDER
        and group.program_requirement.title == DEGREE_REQUIREMENTS
        and SECTION_REFERENCE_RE.search(text) is not None
        and NOT_SECTION_REFERENCE_RE.search(text) is None
    )


def _filter_json(f):
    return {
        'subject': f.subject_id,
        'number_min': f.number_min,
        'number_max': f.number_max,
        'course': f.course_code,
        'exclude': f.exclude,
    }


def _evaluate(group, children_by_parent, taken, ctx, inherited=None):
    """Evaluate a requirement group against the taken courses.

    `ctx` holds what placeholders are checked against (see _evaluate_placeholder):
    "gened", the (json, courses_used) result of evaluating General Education (or
    None); "degree", {requirement id: (json, courses_used)} for the B.A./B.S.
    requirements; "electives", the taken courses available as University electives (or
    None if not yet known); and "sections", the combined status of the program's
    sections other than "Degree Requirements" (or None if not yet known).

    `inherited` is a parent's per-child limit (each_unit, each_min), applied when
    this group doesn't set its own minimum.

    Returns (json, courses_used) where courses_used are the Course objects counted
    toward this group.
    """
    children = children_by_parent.get(group.pk, [])
    node = {
        'id': group.pk,
        'kind': group.kind,
        'text': group.text,
        'condition': group.condition,
        'needs_review': group.needs_review,
        'min_count': group.min_count,
        'max_count': group.max_count,
        'min_credits': group.min_credits,
        'max_credits': group.max_credits,
    }

    if group.kind == Kind.NOTE:
        node['status'] = None
        node['children'] = [
            _evaluate(c, children_by_parent, taken, ctx)[0] for c in children
        ]
        return node, []

    if group.kind == Kind.PLACEHOLDER:
        node['children'] = []
        return _evaluate_placeholder(group, node, ctx)

    child_inherited = (group.each_unit, group.each_min) if group.each_min else None
    child_results = [
        _evaluate(c, children_by_parent, taken, ctx, child_inherited)
        for c in children
    ]
    node['children'] = [r[0] for r in child_results]
    # Notes are informational and never count toward a requirement.
    counted = [r for r in child_results if r[0]['kind'] != Kind.NOTE]

    listed = [cr.course for cr in group.course_requirements.all()]
    node['courses'] = [
        {**course_json(c), 'taken': c.pk in taken} for c in listed
    ]
    filters = list(group.course_filters.all())
    if filters:
        node['filters'] = [_filter_json(f) for f in filters]

    # Taken courses that count directly toward this group, deduplicated.
    direct = {c.pk: c for c in listed if c.pk in taken}
    for c in _filtered_courses(filters, taken.values()):
        direct.setdefault(c.pk, c)
    direct = list(direct.values())

    used = list(direct)
    for child_node, child_used in counted:
        if child_node['status'] == SATISFIED:
            used.extend(child_used)

    if group.kind == Kind.ALL:
        statuses = [r[0]['status'] for r in counted]
        if any(c.pk not in taken for c in listed):
            statuses.append(UNSATISFIED)
        if filters:
            # An "all" group with filters needs at least one matching course.
            statuses.append(SATISFIED if direct else UNSATISFIED)
        status = combine(statuses)
        status = _apply_inherited(status, inherited, used)
    else:  # Kind.CHOOSE
        status = _evaluate_choose(group, counted, direct, used, inherited)

    node['status'] = status
    node['satisfied_by'] = sorted({c.code for c in used})
    return node, used


def _evaluate_placeholder(group, node, ctx):
    gened = ctx['gened']
    if gened and gen_ed.is_gened_placeholder(group.text):
        gened_json, gened_used = gened
        node['general_education'] = True
        node['status'] = gened_json['status']
        node['satisfied_by'] = gened_json['satisfied_by']
        return node, gened_used

    cluster_id = gen_ed.cluster_id_for(group.text)
    if gened and cluster_id:
        gened_json, gened_used = gened
        cluster = next(c for c in gened_json['clusters'] if c['id'] == cluster_id)
        codes = {code for a in cluster['areas'] for code in a['satisfied_by']}
        node['general_education'] = cluster_id
        node['status'] = cluster['status']
        node['satisfied_by'] = sorted(codes)
        return node, [c for c in gened_used if c.code in codes]

    degree_id = _degree_requirement_id(group)
    if degree_id in ctx['degree']:
        degree_json, degree_used = ctx['degree'][degree_id]
        node['degree_requirement'] = degree_id
        node['status'] = degree_json['status']
        node['satisfied_by'] = degree_json['satisfied_by']
        return node, degree_used

    if ctx['electives'] is not None and _is_university_electives(group):
        electives = ctx['electives']
        credits = sum(course_credits(c) for c in electives)
        node['university_electives'] = True
        node['credits_earned'] = credits
        node['status'] = (
            SATISFIED if credits >= (group.min_credits or 0) else UNSATISFIED
        )
        node['satisfied_by'] = sorted(c.code for c in electives)
        return node, electives

    if ctx['sections'] is not None and _is_section_reference(group):
        # The referenced courses are listed (and counted) in those sections.
        node['references_sections'] = True
        node['status'] = ctx['sections']
        node['satisfied_by'] = []
        return node, []

    node['status'] = UNVERIFIED
    node['satisfied_by'] = []
    return node, []


def _apply_inherited(status, inherited, used):
    if status != SATISFIED or not inherited:
        return status
    unit, minimum = inherited
    amount = (
        sum(course_credits(c) for c in used)
        if unit == EachUnit.CREDITS
        else len(used)
    )
    return SATISFIED if amount >= minimum else UNSATISFIED


def _evaluate_choose(group, counted, direct, used, inherited):
    satisfied_children = [r for r in counted if r[0]['status'] == SATISFIED]
    count = len(direct) + len(satisfied_children)
    credits = sum(course_credits(c) for c in used)

    min_count, min_credits = group.min_count, group.min_credits
    if min_count is None and min_credits is None:
        if inherited:
            unit, minimum = inherited
            if unit == EachUnit.CREDITS:
                min_credits = minimum
            else:
                min_count = minimum
        else:
            min_count = 1

    met = (min_count is None or count >= min_count) and (
        min_credits is None or credits >= min_credits
    )
    if met:
        return SATISFIED
    # Can't rule it out if an unverifiable option could make up the difference.
    if any(r[0]['status'] == UNVERIFIED for r in counted):
        return UNVERIFIED
    return UNSATISFIED


def _claimed_codes(node, codes):
    """Collect the codes of courses counted anywhere in an evaluated tree."""
    codes.update(node.get('satisfied_by', []))
    for child in node.get('children', []):
        _claimed_codes(child, codes)
    return codes


def _program_codes(node, codes):
    """Like _claimed_codes, but only courses the program itself lists or filters
    for; placeholders (General Education, B.A./B.S. requirements, University
    electives, section references) are shared by every program, so skipped."""
    if node['kind'] == Kind.PLACEHOLDER:
        return codes
    codes.update(node.get('satisfied_by', []))
    for child in node.get('children', []):
        _program_codes(child, codes)
    return codes


def evaluate_program(program, track_id, taken, get_gened):
    """Evaluate one program's requirements against the taken courses.

    `track_id` limits the requirements to those common to the program or specific
    to that track (or None for all). `get_gened()` returns the (json, courses_used)
    result of evaluating General Education, which is shared by every program.

    Returns (json, program_codes, requires_gened) where program_codes are the codes
    of courses the program's own requirements count (see _program_codes).
    """
    reqs = ProgramRequirement.objects.filter(program=program).select_related('track')
    if track_id:
        reqs = reqs.filter(track__isnull=True) | reqs.filter(track_id=track_id)

    groups = RequirementGroup.objects.filter(
        program_requirement__in=reqs
    ).select_related('program_requirement').prefetch_related(
        Prefetch(
            'course_requirements',
            queryset=CourseRequirement.objects.select_related('course'),
        ),
        Prefetch('course_filters', queryset=CourseFilter.objects.order_by('pk')),
    )
    children_by_parent = defaultdict(list)
    roots = {}
    gened = None
    has_electives = False
    degree_ids = set()
    for g in groups:
        has_electives = has_electives or _is_university_electives(g)
        degree_id = _degree_requirement_id(g)
        if degree_id:
            degree_ids.add(degree_id)
        if (
            gened is None
            and g.kind == Kind.PLACEHOLDER
            and (gen_ed.is_gened_placeholder(g.text) or gen_ed.cluster_id_for(g.text))
        ):
            gened = get_gened()
        if g.parent_group_id is None:
            roots[g.program_requirement_id] = g
        else:
            children_by_parent[g.parent_group_id].append(g)

    reqs = [req for req in reqs if req.pk in roots]
    degree = {}
    if degree_ids:
        degree = degree_reqs.evaluate(degree_ids, taken, get_gened()[1])

    def evaluate_all(ctx):
        """Evaluate every requirement, the program's own sections before the
        "Degree Requirements" that summarize them. Returns {req pk: tree}."""
        trees = {}
        statuses = []
        for req in reqs:
            if req.title == DEGREE_REQUIREMENTS:
                continue
            tree, _ = _evaluate(roots[req.pk], children_by_parent, taken, ctx)
            trees[req.pk] = tree
            if INFORMATIONAL_SECTION_RE.search(req.title):
                continue
            if req.track_id is not None and not track_id:
                # Without a chosen track we can't tell which concentration counts.
                statuses.append(UNVERIFIED)
                continue
            statuses.append(tree['status'] or SATISFIED)
        ctx['sections'] = combine(statuses) if statuses else UNVERIFIED
        for req in reqs:
            if req.title == DEGREE_REQUIREMENTS:
                trees[req.pk], _ = _evaluate(
                    roots[req.pk], children_by_parent, taken, ctx
                )
        return trees

    ctx = {'gened': gened, 'degree': degree, 'electives': None, 'sections': None}
    electives = None
    if has_electives:
        # First pass: find the courses every other requirement counts; the rest
        # are University electives. Courses another of the student's programs
        # counts are still electives here, as they fill this degree's credits.
        claimed = set()
        for tree in evaluate_all(ctx).values():
            _claimed_codes(tree, claimed)
        electives = [c for c in taken.values() if c.code not in claimed]
        ctx['electives'] = electives
    trees = evaluate_all(ctx)

    buckets = {SATISFIED: [], UNSATISFIED: [], UNVERIFIED: []}
    program_codes = set()
    for req in reqs:
        tree = trees[req.pk]
        if not INFORMATIONAL_SECTION_RE.search(req.title):
            _program_codes(tree, program_codes)
        # A requirement that is only a note has nothing to check.
        status = tree['status'] or SATISFIED
        buckets[status].append(
            {
                'id': req.pk,
                'title': req.title,
                'subsection': req.subsection,
                'cluster': req.cluster,
                'track': {'id': req.track.pk, 'name': req.track.name} if req.track else None,
                'credits_min': req.credits_min,
                'credits_max': req.credits_max,
                'status': status,
                'rules': tree,
            }
        )

    statuses = [r['status'] for b in buckets.values() for r in b]
    result = {
        'program': {'slug': program.slug, 'name': program.name, 'type': program.type},
        'track': int(track_id) if track_id else None,
        'status': combine(statuses),
        'satisfied': buckets[SATISFIED],
        'unsatisfied': buckets[UNSATISFIED],
        'unverified': buckets[UNVERIFIED],
        # Only present for B.A./B.S. programs.
        'degree_requirements': [j for j, _ in degree.values()] if degree else None,
        # Only present for programs with a University electives requirement.
        'university_electives': (
            {
                'credits_earned': sum(course_credits(c) for c in electives),
                'satisfied_by': sorted(c.code for c in electives),
            }
            if electives is not None
            else None
        ),
    }
    return result, program_codes, gened is not None
