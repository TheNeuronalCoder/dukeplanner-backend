import re
from collections import defaultdict

from django.db.models import Case, IntegerField, Q, TextField, Value, When
from django.db.models.functions import Cast
from django.http import JsonResponse
from django.views.decorators.http import require_GET

from .models import Course, Program, Track
from .utils import gened as gen_ed
from .utils.courses import course_json, lookup_courses
from .utils.requirements import evaluate_program

SEARCH_DEFAULT_LIMIT = 20
SEARCH_MAX_LIMIT = 100


def _parse_list(request, name):
    """Values of a comma-separated and/or repeated query parameter, deduplicated."""
    values = []
    for value in request.GET.getlist(name):
        values.extend(part.strip() for part in value.split(','))
    return list(dict.fromkeys(v for v in values if v))


def _parse_search(request):
    """(query, limit) from `q` and `limit`, or (None, error response)."""
    query = ' '.join(request.GET.get('q', '').split())
    if not query:
        return None, JsonResponse({'error': "Missing 'q' parameter."}, status=400)
    limit = request.GET.get('limit', str(SEARCH_DEFAULT_LIMIT))
    if not limit.isdigit() or int(limit) < 1:
        return None, JsonResponse(
            {'error': "'limit' must be a positive integer."}, status=400
        )
    return query, min(int(limit), SEARCH_MAX_LIMIT)


def _all_terms(fields, terms):
    """Q matching rows where every term appears (case-insensitively) in some field."""
    q = Q()
    for term in terms:
        any_field = Q()
        for field in fields:
            any_field |= Q(**{f'{field}__icontains': term})
        q &= any_field
    return q


@require_GET
def search_courses(request):
    """GET /search/course?q=<text>[&subject=<code>[,<code>...]][&limit=<n>]

    Matches course codes ("COB 191", "cob19", "COB") and titles. Every word of a
    title search must appear in the title. Results are ranked exact code, then
    code prefix, then title match, and capped at `limit` (default 20, max 100).
    """
    query, limit = _parse_search(request)
    if query is None:
        return limit

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
    subjects = [s.upper() for s in _parse_list(request, 'subject')]
    if subjects:
        courses = courses.filter(prefix__in=subjects)
    courses = courses.annotate(
        rank=Case(
            When(exact, then=Value(0)),
            When(prefix, then=Value(1)),
            When(title__istartswith=query, then=Value(2)),
            default=Value(3),
            output_field=IntegerField(),
        )
    ).order_by('rank', 'prefix', 'number')

    return JsonResponse(
        {'results': [course_json(c) for c in courses[:limit]]}
    )


@require_GET
def search_programs(request):
    """GET /search/program?q=<text>[&level=<level>][&type=<type>[,<type>...]][&limit=<n>]

    Every word of `q` must appear in the program's name or keywords. Results are
    ranked name-prefix, then name, then keyword-only matches, and capped at
    `limit` (default 20, max 100). Each result lists its tracks, whose ids can be
    passed to /requirements.
    """
    query, limit = _parse_search(request)
    if query is None:
        return limit

    programs = Program.objects.annotate(
        keywords_text=Cast('keywords', TextField())
    ).filter(_all_terms(['name', 'keywords_text'], query.split()))
    level = request.GET.get('level')
    if level:
        if level not in Program.Level.values:
            return JsonResponse(
                {'error': f"Unknown level '{level}'. Expected one of: "
                          f"{', '.join(Program.Level.values)}."},
                status=400,
            )
        programs = programs.filter(level=level)
    types = _parse_list(request, 'type')
    if types:
        programs = programs.filter(type__in=types)
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
                for p in programs[:limit]
            ]
        }
    )


@require_GET
def requirements(request):
    """GET /requirements?program=<slug>[,<slug>...]&courses=COB 191,COB 202[&track=<id>[,<id>...]]

    `program`, `courses` and `track` may be comma-separated and/or repeated, so a
    double major or a major with minors is checked in one request. Each track id
    applies to the program it belongs to (at most one per program); if a program
    has a track, only requirements common to it or specific to that track are
    returned.

    `programs` holds one entry per program, in the order given, each with its
    requirements bucketed by status. `degree_requirements` holds the B.A./B.S.
    requirements (philosophy, foreign language, quantitative, scientific literacy)
    the program's placeholders name. University electives are the taken courses no
    other requirement of that program counts. Placeholders like "Major
    requirements" in "Degree Requirements" take the combined status of the
    program's other sections.

    `general_education` holds the cluster/area breakdown when any program requires
    General Education; Gen Ed placeholder groups are checked against it.
    `shared_courses` maps each course counted by more than one program's own
    requirements to those programs' slugs, for checking overlap limits.
    """
    slugs = _parse_list(request, 'program')
    if not slugs:
        return JsonResponse({'error': "Missing 'program' parameter."}, status=400)
    programs = Program.objects.in_bulk(slugs)
    missing = [s for s in slugs if s not in programs]
    if missing:
        return JsonResponse(
            {'error': f"Unknown program(s): {', '.join(missing)}."}, status=404
        )

    track_ids = _parse_list(request, 'track')
    bad = [t for t in track_ids if not t.isdigit()]
    tracks = Track.objects.filter(
        pk__in=[t for t in track_ids if t.isdigit()], program__in=slugs
    ).in_bulk()
    bad += [t for t in track_ids if t.isdigit() and int(t) not in tracks]
    if bad:
        return JsonResponse(
            {'error': f"Unknown track(s) for the given programs: {', '.join(bad)}."},
            status=404,
        )
    track_by_program = {}
    for track in tracks.values():
        if track.program_id in track_by_program:
            return JsonResponse(
                {'error': f"Multiple tracks given for program '{track.program_id}'."},
                status=400,
            )
        track_by_program[track.program_id] = str(track.pk)

    taken, unknown = lookup_courses(_parse_list(request, 'courses'))

    gened = None

    def get_gened():
        nonlocal gened
        if gened is None:
            gened = gen_ed.evaluate(taken)
        return gened

    results = []
    used_by = defaultdict(list)
    requires_gened = False
    for slug in slugs:
        result, codes, program_gened = evaluate_program(
            programs[slug], track_by_program.get(slug), taken, get_gened
        )
        results.append(result)
        requires_gened = requires_gened or program_gened
        for code in codes:
            used_by[code].append(slug)

    return JsonResponse(
        {
            'programs': results,
            'courses': [course_json(c) for c in taken.values()],
            'unknown_courses': unknown,
            # Only present if some program requires General Education.
            'general_education': gened[0] if requires_gened else None,
            'shared_courses': {
                code: slugs_ for code, slugs_ in sorted(used_by.items())
                if len(slugs_) > 1
            },
        }
    )
