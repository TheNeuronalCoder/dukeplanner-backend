from collections import Counter, defaultdict

from django.db import transaction
from django.db.models import Q, Sum
from django.db.models.functions import Coalesce
from django.http import JsonResponse
from rest_framework import status
from rest_framework.decorators import api_view, parser_classes, permission_classes
from rest_framework.parsers import JSONParser
from rest_framework.permissions import IsAuthenticated

from account.validation import errors, validate_account
from catalog.utils import gened as gen_ed
from catalog.utils.courses import course_json
from catalog.utils.requirements import evaluate_program

from .models import Class, Curriculum, Semester
from .serializers import (
    AddCourseSerializer,
    AddCoursesSerializer,
    AddProgramSerializer,
    AddProgramsSerializer,
    AddSemesterSerializer,
    AddSemestersSerializer,
    add_serializer,
)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def courses(request):
    """GET /student/courses/

    Returns the signed-in account's semesters in chronological order, each with
    the courses taken in it. Classes not yet placed in a semester are listed
    under `unassigned`. Each course's `credits` and `credits_earned` (false if
    failed) are the student's; a semester's `credits_attempted` sums its classes'
    credits and `credits_earned` sums those of the classes that were passed.
    """
    account = request.user
    classes = account.classes.select_related('course').order_by('course__prefix_id', 'course__number')

    by_semester = defaultdict(list)
    for c in classes:
        # `id` is the Class pk, used by DELETE /student/courses/delete/.
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

    return JsonResponse(
        {
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
    )


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def requirements(request):
    """GET /student/requirements/

    Checks the signed-in account's programs (Curriculum, with its track if one is
    chosen) against its passed classes, so a double major or a major with minors is
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


def _issue_json(i):
    return {'code': i.code, 'message': i.message, 'severity': i.severity.value}


def _issues_json(account):
    return [_issue_json(i) for i in validate_account(account)]


class _Invalidates(Exception):
    """Raised inside a change's transaction to roll it back."""

    def __init__(self, issues):
        self.issues = issues


def _new_errors(account, before):
    """The account's ERROR-level issues that aren't in `before` (a Counter of
    (code, message)), so an account still being set up isn't blocked by errors
    it already had, only by ones the change introduces.
    """
    remaining = before.copy()
    new = []
    for i in errors(validate_account(account)):
        key = (i.code, i.message)
        if remaining[key]:
            remaining[key] -= 1
        else:
            new.append(i)
    return new


def _guarded(account, change):
    """Runs `change()` in a transaction and returns (its result, None), or rolls
    it back and returns (None, a 409 response) if it would make the account
    invalid, i.e. introduce new ERROR-level validation issues.
    """
    try:
        with transaction.atomic():
            before = Counter((i.code, i.message) for i in errors(validate_account(account)))
            result = change()
            new = _new_errors(account, before)
            if new:
                raise _Invalidates(new)
    except _Invalidates as e:
        return None, JsonResponse(
            {
                'error': 'This change would make the account invalid.',
                'issues': [_issue_json(i) for i in e.issues],
            },
            status=status.HTTP_409_CONFLICT,
        )
    return result, None


def _added(account, objs, noun):
    return JsonResponse(
        {
            'detail': f'{len(objs)} {noun} added.',
            'ids': [o.pk for o in objs],
            'issues': _issues_json(account),
        },
        status=status.HTTP_201_CREATED,
    )


def _delete_owned(request, queryset, noun, field='pk', delete=None):
    """Deletes every row of `queryset` (already limited to the caller's account)
    whose `field` is in the request body, a non-empty JSON list. If any value
    isn't found, nothing is deleted and they're returned with a 404. If the
    deletion would make the account invalid, nothing is deleted and the new
    errors are returned with a 409. `delete`, if given, is called with the
    matching queryset in place of deleting it directly.
    """
    pks = request.data
    if not isinstance(pks, list) or not pks or not all(isinstance(pk, (int, str)) for pk in pks):
        return JsonResponse(
            {'error': 'Body must be a non-empty list of ids.'}, status=status.HTTP_400_BAD_REQUEST
        )
    pks = list(dict.fromkeys(pks))

    targets = queryset.filter(**{f'{field}__in': pks})
    found = {str(pk) for pk in targets.values_list(field, flat=True)}
    missing = [pk for pk in pks if str(pk) not in found]
    if missing:
        return JsonResponse(
            {'error': f'Some {noun} were not found.', 'missing': missing},
            status=status.HTTP_404_NOT_FOUND,
        )

    _, rejected = _guarded(request.user, lambda: delete(targets) if delete else targets.delete())
    if rejected:
        return rejected

    return JsonResponse(
        {'detail': f'{len(pks)} {noun} deleted.', 'issues': _issues_json(request.user)}
    )


@api_view(['POST'])
@parser_classes([JSONParser])
@permission_classes([IsAuthenticated])
def add_courses(request):
    """POST /student/courses/add/

    Adds classes to the signed-in account. `semester` is the pk of one of the
    account's semesters. Omitting it or passing null leaves the class unassigned,
    which validation flags as an error, so it's rejected (see below).
    `credits` defaults to the course's credits and must be within its range, and
    `credits_earned` (false if failed) defaults to true. A
    course can't be listed twice in the same semester. Nothing is saved if any
    item is invalid.

    Returns the new class ids (in request order) and the account's validation
    issues. Returns a 409 with the new errors, saving nothing, if the classes
    would make the account invalid (see account.validation).

    Sample request body:
    [
        {"course": "CS 149", "semester": 3},
        {"course": "IND 499", "semester": 3, "credits": 2, "credits_earned": false},
        {"course": "MATH 235"}
    ]
    """
    serializer = add_serializer(AddCourseSerializer, AddCoursesSerializer, request.data, request)
    serializer.is_valid(raise_exception=True)

    account = request.user
    classes, rejected = _guarded(account, lambda: Class.objects.bulk_create(
        Class(
            account=account,
            course=i['course'],
            semester=i['semester'],
            credits=i['credits'],
            credits_earned=i['credits_earned'],
        )
        for i in serializer.validated_data
    ))
    if rejected:
        return rejected
    return _added(account, classes, 'courses')


@api_view(['POST'])
@parser_classes([JSONParser])
@permission_classes([IsAuthenticated])
def add_programs(request):
    """POST /student/programs/add/

    Adds programs (by slug) to the signed-in account, each with an optional
    track pk that must belong to that program. A program the account already
    has can't be added again. Nothing is saved if any item is invalid.

    Returns the new program slugs and the account's validation issues. Returns
    a 409 with the new errors, saving nothing, if the programs would make the
    account invalid (e.g. a minor in the same area as one of its majors).

    Sample request body:
    [
        {"program": "computer-science-bs"},
        {"program": "mathematics-minor", "track": 12}
    ]
    """
    serializer = add_serializer(AddProgramSerializer, AddProgramsSerializer, request.data, request)
    serializer.is_valid(raise_exception=True)

    account = request.user
    _, rejected = _guarded(account, lambda: Curriculum.objects.bulk_create(
        Curriculum(account=account, program=i['program'], track=i['track'])
        for i in serializer.validated_data
    ))
    if rejected:
        return rejected
    programs = [i['program'] for i in serializer.validated_data]
    return _added(account, programs, 'programs')


@api_view(['POST'])
@parser_classes([JSONParser])
@permission_classes([IsAuthenticated])
def add_semesters(request):
    """POST /student/semesters/add/

    Adds semesters to the signed-in account. A term/year the account already
    has can't be added again. Nothing is saved if any item is invalid.

    Returns the new semester ids (in request order) and the account's
    validation issues. Returns a 409 with the new errors, saving nothing, if the
    semesters would make the account invalid (e.g. a year out of range).

    Sample request body:
    [
        {"term": "fall", "year": 2025},
        {"term": "spring", "year": 2026}
    ]
    """
    serializer = add_serializer(AddSemesterSerializer, AddSemestersSerializer, request.data, request)
    serializer.is_valid(raise_exception=True)

    account = request.user
    semesters, rejected = _guarded(account, lambda: Semester.objects.bulk_create(
        Semester(
            account=account,
            term=Semester.Term[i['term'].upper()],
            year=i['year'],
        )
        for i in serializer.validated_data
    ))
    if rejected:
        return rejected
    return _added(account, semesters, 'semesters')


@api_view(['DELETE'])
@parser_classes([JSONParser])
@permission_classes([IsAuthenticated])
def delete_courses(request):
    """DELETE /student/courses/delete/ [1, 2, 3]

    Deletes the signed-in account's classes with these ids (the `id` of each
    course in GET /student/courses/), along with any semesters left empty,
    except that the account always keeps at least one semester. Returns a 409,
    deleting nothing, if that would make the account invalid.
    """
    account = request.user

    def delete(classes):
        semester_ids = set(classes.exclude(semester=None).values_list('semester_id', flat=True))
        classes.delete()
        emptied = account.semesters.filter(pk__in=semester_ids, classes=None)
        if emptied and emptied.count() == account.semesters.count():
            # Keep the latest so the account isn't left without a semester.
            emptied = emptied.exclude(pk=emptied.order_by('-year', '-term').first().pk)
        emptied.delete()

    return _delete_owned(request, account.classes.all(), 'courses', delete=delete)


@api_view(['DELETE'])
@parser_classes([JSONParser])
@permission_classes([IsAuthenticated])
def delete_programs(request):
    """DELETE /student/programs/delete/ ["computer-science-bs"]

    Removes these programs (by slug) from the signed-in account. Returns a 409,
    removing nothing, if that would make the account invalid (e.g. removing its
    only degree while it still has a minor).
    """
    return _delete_owned(request, request.user.programs.all(), 'programs', field='program_id')


@api_view(['DELETE'])
@parser_classes([JSONParser])
@permission_classes([IsAuthenticated])
def delete_semesters(request):
    """DELETE /student/semesters/delete/ [1, 2]

    Deletes the signed-in account's semesters with these ids, along with the
    classes placed in them. Returns a 409, deleting nothing, if that would make
    the account invalid (e.g. deleting its only semester).
    """
    return _delete_owned(request, request.user.semesters.all(), 'semesters')
