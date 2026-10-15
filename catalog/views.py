import re

from django.db.models import Case, IntegerField, Q, TextField, Value, When
from django.db.models.functions import Cast
from django.http import JsonResponse
from rest_framework.decorators import (
    api_view, authentication_classes, parser_classes, permission_classes,
)
from rest_framework.parsers import JSONParser
from rest_framework.permissions import AllowAny

from .models import Course, Program
from .serializers import CourseSearchSerializer, CoursesSerializer, ProgramSearchSerializer
from .utils.courses import course_json, lookup_courses


def _all_terms(fields, terms):
    """Q matching rows where every term appears (case-insensitively) in some field."""
    q = Q()
    for term in terms:
        any_field = Q()
        for field in fields:
            any_field |= Q(**{f'{field}__icontains': term})
        q &= any_field
    return q


@api_view(['GET'])
@parser_classes([JSONParser])
@authentication_classes([])
@permission_classes([AllowAny])
def courses(request):
    """GET /catalog/courses?codes=<code>[,<code>...]

    Returns {code: course} for each requested code, keyed exactly as given, with
    course details included. Codes that don't match a course are omitted.
    """
    serializer = CoursesSerializer(data=request.query_params)
    serializer.is_valid(raise_exception=True)
    codes = serializer.validated_data['codes']

    found, _ = lookup_courses(codes)
    by_key = {(c.prefix_id, c.number.upper()): c for c in found.values()}

    results = {}
    for code in codes:
        match = re.fullmatch(r'([A-Za-z]+)\s*(\S+)', code)
        course = match and by_key.get((match.group(1).upper(), match.group(2).upper()))
        if course:
            results[code] = course_json(course, details=True)
    return JsonResponse(results)


@api_view(['GET'])
@parser_classes([JSONParser])
@authentication_classes([])
@permission_classes([AllowAny])
def search_courses(request):
    """GET /catalog/search/course?q=<text>[&subject=<code>[,<code>...]][&limit=<n>]

    Matches course codes ("COB 191", "cob19", "COB") and titles. Every word of a
    title search must appear in the title. Results are ranked exact code, then
    code prefix, then title match, and capped at `limit` (default 20, max 100).
    """
    serializer = CourseSearchSerializer(data=request.query_params)
    serializer.is_valid(raise_exception=True)
    params = serializer.validated_data
    query = params['q']

    code_match = re.fullmatch(r'([A-Za-z]+)\s*(\d\w*)?', query)
    exact = prefix = Q(pk__in=[])
    if code_match:
        subject, number = code_match.group(1).upper(), code_match.group(2)
        if number:
            exact = Q(prefix=subject, number__iexact=number)
            prefix = Q(prefix=subject, number__istartswith=number)
        else:
            prefix = Q(prefix=subject)
    title = _all_terms(['title'], query.split())

    courses = Course.objects.filter(exact | prefix | title)
    if params['subject']:
        courses = courses.filter(prefix__in=params['subject'])
    courses = courses.annotate(
        rank=Case(
            When(exact, then=Value(0)),
            When(prefix, then=Value(1)),
            When(title__istartswith=query, then=Value(2)),
            default=Value(3),
            output_field=IntegerField(),
        )
    ).order_by('rank', 'prefix', 'number')

    return JsonResponse({'results': [course_json(c) for c in courses[:params['limit']]]})


@api_view(['GET'])
@parser_classes([JSONParser])
@authentication_classes([])
@permission_classes([AllowAny])
def search_programs(request):
    """GET /catalog/search/program?q=<text>[&level=<level>][&type=<type>[,<type>...]][&limit=<n>]

    Every word of `q` must appear in the program's name or keywords. Results are
    ranked name-prefix, then name, then keyword-only matches, and capped at
    `limit` (default 20, max 100). Each result lists its tracks.
    """
    serializer = ProgramSearchSerializer(data=request.query_params)
    serializer.is_valid(raise_exception=True)
    params = serializer.validated_data
    query = params['q']

    programs = Program.objects.annotate(
        keywords_text=Cast('keywords', TextField())
    ).filter(_all_terms(['name', 'keywords_text'], query.split()))
    if 'level' in params:
        programs = programs.filter(level=params['level'])
    if params['type']:
        programs = programs.filter(type__in=params['type'])
    programs = (
        programs.annotate(
            rank=Case(
                When(name__istartswith=query, then=Value(0)),
                When(name__icontains=query, then=Value(1)),
                When(_all_terms(['name'], query.split()), then=Value(2)),
                default=Value(3),
                output_field=IntegerField(),
            )
        )
        .order_by('rank', 'name')
        .prefetch_related('tracks')
    )

    return JsonResponse(
        {
            'results': [
                {
                    'slug': p.slug,
                    'name': p.name,
                    'level': p.level,
                    'type': p.type,
                    'url': p.url,
                    'tracks': [{'id': t.pk, 'name': t.name} for t in p.tracks.all()],
                }
                for p in programs[:params['limit']]
            ]
        }
    )

