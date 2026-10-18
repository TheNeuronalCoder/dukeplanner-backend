from collections import Counter

from django.conf import settings
from django.db import transaction
from django.http import JsonResponse
from rest_framework import status
from rest_framework.decorators import api_view, parser_classes, permission_classes
from rest_framework.parsers import JSONParser
from rest_framework.permissions import IsAuthenticated

from account.validation import errors, validate_account
from .models import Class, Curriculum, Semester
from .serializers import (
    AddCourseSerializer,
    AddCoursesSerializer,
    AddProgramSerializer,
    AddProgramsSerializer,
    AddSemesterSerializer,
    AddSemestersSerializer,
    UpdateCourseSerializer,
    UpdateCoursesSerializer,
    list_serializer,
)
from .summary import requirements_data, semesters_data


def _dispatch(request, handlers):
    return handlers[request.method](request)


@api_view(['POST', 'PATCH', 'DELETE'])
@parser_classes([JSONParser])
@permission_classes([IsAuthenticated])
def courses(request):
    """/student/courses/

    POST adds classes to the signed-in account, PATCH updates them and DELETE
    removes them (see _add_courses, _update_courses and _delete_courses). The
    classes are listed under `classes` in GET /student/requirements/.
    """
    return _dispatch(request, {
        'POST': _add_courses,
        'PATCH': _update_courses,
        'DELETE': _delete_courses,
    })


@api_view(['POST', 'DELETE'])
@parser_classes([JSONParser])
@permission_classes([IsAuthenticated])
def programs(request):
    """/student/programs/

    POST adds programs to the signed-in account and DELETE removes them (see
    _add_programs and _delete_programs).
    """
    return _dispatch(request, {
        'POST': _add_programs,
        'DELETE': _delete_programs
    })


@api_view(['POST', 'DELETE'])
@parser_classes([JSONParser])
@permission_classes([IsAuthenticated])
def semesters(request):
    """/student/semesters/

    POST adds semesters (with their classes) to the signed-in account and DELETE
    removes them (see _add_semesters and _delete_semesters).
    """
    return _dispatch(request, {
        'POST': _add_semesters,
        'DELETE': _delete_semesters
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def requirements(request):
    """GET /student/requirements/

    Checks the signed-in account's programs (Curriculum, with its track if one is
    chosen) against its passed classes, so a double major or a major with minors is
    checked together. If a program has a track, only requirements common to it or
    specific to that track are returned.

    `programs` holds one entry per program, in the order they were added, each
    with its requirements bucketed by status: "satisfied" by passed classes of
    completed semesters (or ones not placed in a semester), "in progress" if the
    classes of the CURRENT_SEMESTER would satisfy them once passed, "planned" if
    classes of later semesters would, otherwise "unsatisfied" or "unverified".
    `degree_requirements` holds the
    B.A./B.S. requirements (philosophy, foreign language, quantitative, scientific
    literacy) the program's placeholders name. University electives are the taken
    courses no other requirement of that program counts. Placeholders like "Major
    requirements" in "Degree Requirements" take the combined status of the
    program's other sections.

    `general_education` holds the cluster/area breakdown when any program requires
    General Education; Gen Ed placeholder groups are checked against it.
    `shared_courses` maps each course counted by more than one program's own
    requirements to those programs' slugs, for checking overlap limits.

    `classes` holds the account's semesters in chronological order, each with
    the courses taken in it; classes not yet placed in a semester are listed
    under its `unassigned`. Each course's `id` is its class id (used by PATCH
    and DELETE /student/courses/), and its `credits` and `credits_earned` (false
    if failed) are the student's; a semester's `credits_attempted` sums its
    classes' credits and `credits_earned` sums those of the classes that were
    passed. A semester's `status` is "completed", "in progress" or "planned"
    relative to the CURRENT_SEMESTER setting.

    Returns a 409 with the ERROR-level issues and `classes` if the account isn't
    set up and valid (see account.validation).
    """
    classes = semesters_data(request.user, settings.CURRENT_SEMESTER)
    data, issues = requirements_data(request.user, settings.CURRENT_SEMESTER)
    if issues:
        return JsonResponse(
            {
                'error': 'Account is not set up or is invalid.',
                'issues': [_issue_json(i) for i in issues],
                'classes': classes,
            },
            status=status.HTTP_409_CONFLICT,
        )
    return JsonResponse({**data, 'classes': classes})


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


def _updated(account, ids, noun):
    return JsonResponse(
        {'detail': f'{len(ids)} {noun} updated.', 'ids': ids, 'issues': _issues_json(account)}
    )


def _delete_emptied(account, semester_ids):
    """Deletes the account's semesters with these ids that no longer have any
    classes, since semesters can't be empty."""
    account.semesters.filter(pk__in=semester_ids, classes=None).delete()


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


def _add_courses(request):
    """POST /student/courses/

    Adds classes to the signed-in account. `semester` is the id of one of the
    account's semesters (use POST /student/semesters/ to add a class in a new
    one). `credits` defaults to the course's credits and must be 1-9 (it
    needn't match the catalog), and `credits_earned` (false if failed) defaults
    to true. A course can't be listed twice in the same semester. Nothing is
    saved if any item is invalid.

    Returns the new class ids (in request order) and the account's validation
    issues. Returns a 409 with the new errors, saving nothing, if the classes
    would make the account invalid (see account.validation).

    Sample request body:
    [
        {"course": "CS 149", "semester": 3},
        {"course": "IND 499", "semester": 3, "credits": 2, "credits_earned": false}
    ]
    """
    serializer = list_serializer(AddCourseSerializer, AddCoursesSerializer, request.data, request)
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


def _update_courses(request):
    """PATCH /student/courses/

    Updates the signed-in account's classes: each item has the class `id` and
    any of `course`, `semester`, `credits` and `credits_earned` to change.
    Changing `course` without giving `credits` resets them to the new course's
    credits. Semesters left without classes by a move are deleted. Nothing is
    saved if any item is invalid.

    Returns the updated class ids and the account's validation issues. Returns a
    409 with the new errors, saving nothing, if the changes would make the
    account invalid (e.g. a course listed twice in a semester).

    Sample request body:
    [
        {"id": 12, "semester": 4},
        {"id": 13, "credits": 2, "credits_earned": false}
    ]
    """
    serializer = list_serializer(UpdateCourseSerializer, UpdateCoursesSerializer, request.data, request)
    serializer.is_valid(raise_exception=True)

    account = request.user

    def change():
        left = set()
        for item in serializer.validated_data:
            c = item['id']
            if 'semester' in item and item['semester'].pk != c.semester_id:
                left.add(c.semester_id)
            for field, value in item.items():
                if field != 'id':
                    setattr(c, field, value)
            c.save()
        _delete_emptied(account, left)

    _, rejected = _guarded(account, change)
    if rejected:
        return rejected
    return _updated(account, [i['id'].pk for i in serializer.validated_data], 'courses')


def _delete_courses(request):
    """DELETE /student/courses/ [1, 2, 3]

    Deletes the signed-in account's classes with these ids (the `id` of each
    course under `classes` in GET /student/requirements/), along with any semesters left empty.
    Returns a 409, deleting nothing, if that would make the account invalid
    (e.g. deleting every class of its only semester).
    """
    account = request.user

    def delete(classes):
        semester_ids = set(classes.exclude(semester=None).values_list('semester_id', flat=True))
        classes.delete()
        _delete_emptied(account, semester_ids)

    return _delete_owned(request, account.classes.all(), 'courses', delete=delete)


def _add_programs(request):
    """POST /student/programs/

    Adds programs (by slug) to the signed-in account, each with an optional
    track id that must belong to that program. A program the account already
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
    serializer = list_serializer(AddProgramSerializer, AddProgramsSerializer, request.data, request)
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


def _delete_programs(request):
    """DELETE /student/programs/ ["computer-science-bs"]

    Removes these programs (by slug) from the signed-in account. Returns a 409,
    removing nothing, if that would make the account invalid (e.g. removing its
    only degree while it still has a minor).
    """
    return _delete_owned(request, request.user.programs.all(), 'programs', field='program_id')


def _add_semesters(request):
    """POST /student/semesters/

    Adds semesters to the signed-in account along with their classes, since a
    semester can't be empty. Each course is an object like those of POST
    /student/courses/ (without `semester`), or a bare course code for the
    defaults. A term/year the account already has can't be added again, and a
    course can't be listed twice in a semester. Nothing is saved if any item is
    invalid.

    Returns the new semester ids (in request order) and the account's
    validation issues. Returns a 409 with the new errors, saving nothing, if the
    semesters would make the account invalid (e.g. a year out of range).

    Sample request body:
    [
        {"term": "fall", "year": 2025, "courses": ["CS 149", {"course": "MATH 235", "credits_earned": false}]},
        {"term": "spring", "year": 2026, "courses": [{"course": "IND 499", "credits": 2}]}
    ]
    """
    serializer = list_serializer(AddSemesterSerializer, AddSemestersSerializer, request.data, request)
    serializer.is_valid(raise_exception=True)

    account = request.user
    items = serializer.validated_data

    def change():
        semesters = Semester.objects.bulk_create(
            Semester(account=account, term=Semester.Term[i['term'].upper()], year=i['year'])
            for i in items
        )
        Class.objects.bulk_create(
            Class(
                account=account,
                semester=s,
                course=c['course'],
                credits=c['credits'],
                credits_earned=c['credits_earned'],
            )
            for s, i in zip(semesters, items)
            for c in i['courses']
        )
        return semesters

    semesters, rejected = _guarded(account, change)
    if rejected:
        return rejected
    return _added(account, semesters, 'semesters')


def _delete_semesters(request):
    """DELETE /student/semesters/ [1, 2]

    Deletes the signed-in account's semesters with these ids, along with the
    classes placed in them. Returns a 409, deleting nothing, if that would make
    the account invalid (e.g. deleting its only semester).
    """
    return _delete_owned(request, request.user.semesters.all(), 'semesters')
