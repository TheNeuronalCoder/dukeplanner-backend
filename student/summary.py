"""The signed-in account's plan as plain data, shared by the student views and
the assistant."""

from collections import defaultdict

from django.db.models import F, Q, Sum
from django.db.models.functions import Coalesce

from account.validation import errors, validate_account
from catalog.utils import gened as gen_ed
from catalog.utils.courses import course_json
from catalog.utils.requirements import bucket, evaluate_program, requirements_of
from catalog.utils.status import IN_PROGRESS, SATISFIED, combine
from .models import Semester


def _semester_key(semester):
    """(year, Semester.Term) for a TERM_YEAR string like "FALL_2026"."""
    term, _, year = semester.partition('_')
    return int(year), Semester.Term[term.upper()]


def _status(year, term, current):
    key = (year, term)
    if key < current:
        return 'completed'
    if key == current:
        return 'in progress'
    return 'planned'


def semesters_data(account, current_semester):
    """The `classes` of GET /student/requirements/ (see student.views.requirements).

    `current_semester` is the semester in progress as TERM_YEAR (e.g.
    "FALL_2026"), which sets each semester's `status`.
    """
    current = _semester_key(current_semester)
    classes = account.classes.select_related('course').order_by('course__prefix_id', 'course__number')

    by_semester = defaultdict(list)
    for c in classes:
        course = c.course
        by_semester[c.semester_id].append({
            'id': c.pk,
            **course_json(course, details=True),
            'credits': c.credits,
            'official_credits': (
                course.credits if course.credits is not None
                else [course.min_credits, course.max_credits]
            ),
            'credits_earned': c.credits_earned,
        })

    semesters = account.semesters.annotate(
        attempted=Coalesce(Sum('classes__credits'), 0),
        earned=Coalesce(Sum('classes__credits', filter=Q(classes__credits_earned=True)), 0),
    ).order_by('year', 'term')

    return {
        'semesters': [
            {
                'id': s.pk,
                'term': s.get_term_display(),
                'year': s.year,
                'status': _status(s.year, s.term, current),
                'credits_attempted': s.attempted,
                'credits_earned': s.earned,
                'courses': by_semester[s.pk],
            }
            for s in semesters
        ],
        'unassigned': by_semester[None],
    }


def _evaluate(curricula, taken):
    """Evaluates the account's programs and General Education against `taken`
    (a dict of pk -> Course)."""
    gened = None

    def get_gened():
        nonlocal gened
        if gened is None:
            gened = gen_ed.evaluate(taken)
        return gened

    results = []
    used_by = defaultdict(list)
    requires_gened = False
    for curriculum in curricula:
        result, codes, program_gened = evaluate_program(
            curriculum.program, curriculum.track_id, taken, get_gened
        )
        results.append(result)
        requires_gened = requires_gened or program_gened
        for code in codes:
            used_by[code].append(curriculum.program_id)

    return {
        'programs': results,
        'courses': [course_json(c) for c in taken.values()],
        # Only present if some program requires General Education.
        'general_education': gened[0] if requires_gened else None,
        'shared_courses': {
            code: slugs for code, slugs in sorted(used_by.items())
            if len(slugs) > 1
        },
    }


def _mark_in_progress(node, done):
    """Changes every `status` in `node` (an evaluation counting the classes in
    progress) that is satisfied there but not in `done` (the same evaluation
    with completed classes only) to IN_PROGRESS. Lists are matched up by `id`
    if their items have one, otherwise by position.
    """
    if isinstance(node, dict) and isinstance(done, dict):
        if node.get('status') == SATISFIED and done.get('status') != SATISFIED:
            node['status'] = IN_PROGRESS
        for key, value in node.items():
            if key in done:
                _mark_in_progress(value, done[key])
    elif isinstance(node, list) and isinstance(done, list):
        if all(isinstance(x, dict) and 'id' in x for x in node + done):
            done_by_id = {x['id']: x for x in done}
            pairs = [(x, done_by_id.get(x['id'])) for x in node]
        else:
            pairs = zip(node, done) if len(node) == len(done) else []
        for x, d in pairs:
            _mark_in_progress(x, d)


def _merge_program(program, done):
    """Marks `program` (see _mark_in_progress) and re-buckets its requirements."""
    requirements = requirements_of(program)
    _mark_in_progress(requirements, requirements_of(done))
    _mark_in_progress(program['degree_requirements'], done['degree_requirements'])
    program.update(bucket(requirements))
    program['status'] = combine([r['status'] for r in requirements])


def requirements_data(account, current_semester):
    """The body of GET /student/requirements/ (see student.views.requirements).

    `current_semester` is the semester in progress as TERM_YEAR (e.g.
    "FALL_2026"). Passed classes of earlier semesters, and ones not placed in a
    semester, count as completed. A requirement that only the classes of the
    current semester would meet is "in progress". Classes of later semesters
    don't count.

    Returns (data, None), or (None, issues) with the account's ERROR-level
    validation issues if it isn't set up and valid.
    """
    issues = errors(validate_account(account))
    if issues:
        return None, issues

    current = _semester_key(current_semester)
    curricula = list(account.programs.select_related('program').order_by('pk'))
    # In the order taken (classes not placed in a semester first), each course
    # once, as first taken, so requirements show the class that met them first.
    taken, completed = {}, {}
    # Failed classes don't count toward any requirement.
    classes = account.classes.filter(credits_earned=True).select_related('course', 'semester').order_by(
        F('semester__year').asc(nulls_first=True), 'semester__term', 'pk'
    )
    for c in classes:
        status = 'completed' if c.semester is None else _status(c.semester.year, c.semester.term, current)
        if status == 'planned' or c.course.pk in taken:
            continue
        # Count the credits the student took, which can differ from the catalog's
        # for variable-credit courses (course_credits reads `credits` first).
        c.course.credits = c.credits
        taken[c.course.pk] = c.course
        if status == 'completed':
            completed[c.course.pk] = c.course

    data = _evaluate(curricula, taken)
    if len(completed) < len(taken):
        done = _evaluate(curricula, completed)
        for program, done_program in zip(data['programs'], done['programs']):
            _merge_program(program, done_program)
        _mark_in_progress(data['general_education'], done['general_education'])
    return data, None
