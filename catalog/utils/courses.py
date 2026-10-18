import re

from ..models import Course


def course_credits(course):
    return course.credits if course.credits is not None else course.min_credits


def course_number(course):
    """Leading numeric part of a course number, e.g. "499A" -> 499."""
    match = re.match(r'\d+', course.number)
    return int(match.group()) if match else None


def normalize_code(code):
    return ' '.join(code.upper().split())


def course_json(course, details=False):
    """`details` adds the description, prerequisites and corequisites."""
    data = {
        'code': course.code,
        'title': course.title,
        'credits': course_credits(course),
        'min_credits': course.min_credits,
        'max_credits': course.max_credits,
    }
    if details:
        data['description'] = course.description
        data['prerequisites'] = course.prerequisites
        data['corequisites'] = course.corequisites
    return data


def lookup_courses(codes):
    """Map course codes like "COB 191" to Course objects; returns (found, unknown)."""
    wanted = {}
    unknown = []
    for code in codes:
        match = re.fullmatch(r'([A-Za-z]+)\s*(\S+)', code)
        if not match:
            unknown.append(code)
            continue
        wanted[(match.group(1).upper(), match.group(2).upper())] = code

    found = {}
    prefixes = {prefix for prefix, _ in wanted}
    for course in Course.objects.filter(prefix__in=prefixes):
        if (course.prefix_id, course.number.upper()) in wanted:
            found[course.pk] = course
    found_keys = {(c.prefix_id, c.number.upper()) for c in found.values()}
    unknown.extend(code for key, code in wanted.items() if key not in found_keys)
    return found, unknown
