from collections import defaultdict

from django.http import JsonResponse
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated

from account.validation import errors, validate_account
from catalog.utils import gened as gen_ed
from catalog.utils.courses import course_json
from catalog.utils.requirements import evaluate_program


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def courses(request):
    """GET /student/courses/

    Returns the signed-in account's semesters in chronological order, each with
    the courses taken in it. Classes not yet placed in a semester are listed
    under `unassigned`.
    """
    account = request.user
    classes = account.classes.select_related('course').order_by('course__prefix_id', 'course__number')

    by_semester = defaultdict(list)
    for c in classes:
        by_semester[c.semester_id].append(course_json(c.course))

    return JsonResponse(
        {
            'semesters': [
                {
                    'id': s.pk,
                    'term': s.get_term_display(),
                    'year': s.year,
                    'credits_attempted': s.credits_attempted,
                    'credits_earned': s.credits_earned,
                    'courses': by_semester[s.pk],
                }
                for s in account.semesters.order_by('year', 'term')
            ],
            'unassigned': by_semester[None],
        }
    )


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def requirements(request):
    """GET /student/requirements/

    Checks the signed-in account's programs (Curriculum, with its track if one is
    chosen) against its classes, so a double major or a major with minors is
    checked together. If a program has a track, only requirements common to it or
    specific to that track are returned.

    `programs` holds one entry per program, in the order they were added, each
    with its requirements bucketed by status. `degree_requirements` holds the
    B.A./B.S. requirements (philosophy, foreign language, quantitative, scientific
    literacy) the program's placeholders name. University electives are the taken
    courses no other requirement of that program counts. Placeholders like "Major
    requirements" in "Degree Requirements" take the combined status of the
    program's other sections.

    `general_education` holds the cluster/area breakdown when any program requires
    General Education; Gen Ed placeholder groups are checked against it.
    `shared_courses` maps each course counted by more than one program's own
    requirements to those programs' slugs, for checking overlap limits.

    Returns a 409 with the ERROR-level issues if the account isn't set up and valid
    (see account.validation).
    """
    account = request.user
    issues = errors(validate_account(account))
    if issues:
        return JsonResponse(
            {
                'error': 'Account is not set up or is invalid.',
                'issues': [
                    {'code': i.code, 'message': i.message, 'severity': i.severity.value}
                    for i in issues
                ],
            },
            status=status.HTTP_409_CONFLICT,
        )

    curricula = account.programs.select_related('program').order_by('pk')
    taken = {c.course.pk: c.course for c in account.classes.select_related('course')}

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

    return JsonResponse(
        {
            'programs': results,
            'courses': [course_json(c) for c in taken.values()],
            # Only present if some program requires General Education.
            'general_education': gened[0] if requires_gened else None,
            'shared_courses': {
                code: slugs for code, slugs in sorted(used_by.items())
                if len(slugs) > 1
            },
        }
    )
