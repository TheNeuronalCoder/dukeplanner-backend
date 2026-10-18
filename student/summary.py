"""The signed-in account's plan as plain data, shared by the student views and
the assistant."""

from collections import defaultdict

from django.db.models import Q, Sum
from django.db.models.functions import Coalesce

from account.validation import errors, validate_account
from catalog.utils import gened as gen_ed
from catalog.utils.courses import course_json
from catalog.utils.requirements import evaluate_program


def semesters_data(account):
    """The `classes` of GET /student/requirements/ (see student.views.requirements)."""
    classes = account.classes.select_related('course').order_by('course__prefix_id', 'course__number')

    by_semester = defaultdict(list)
    for c in classes:
        # `id` is the Class pk, used by PATCH and DELETE /student/courses/.
        by_semester[c.semester_id].append({
            'id': c.pk,
            **course_json(c.course, details=True),
            'credits': c.credits,
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
                'credits_attempted': s.attempted,
                'credits_earned': s.earned,
                'courses': by_semester[s.pk],
            }
            for s in semesters
        ],
        'unassigned': by_semester[None],
    }


def requirements_data(account):
    """The body of GET /student/requirements/ (see student.views.requirements).

    Returns (data, None), or (None, issues) with the account's ERROR-level
    validation issues if it isn't set up and valid.
    """
    issues = errors(validate_account(account))
    if issues:
        return None, issues

    curricula = account.programs.select_related('program').order_by('pk')
    taken = {}
    # Failed classes don't count toward any requirement.
    for c in account.classes.filter(credits_earned=True).select_related('course'):
        # Count the credits the student took, which can differ from the catalog's
        # for variable-credit courses (course_credits reads `credits` first).
        c.course.credits = c.credits
        taken[c.course.pk] = c.course

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
    }, None
