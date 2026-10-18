"""Account setup validation for Duke Planner.

An account is "valid and set up" when it has no ERROR-level issues:

  1. It pursues at least one degree (not just a minor/certificate).
  2. Its programs form a combination JMU allows.
  3. It has at least one semester, and every semester is sane and has classes.
  4. Every class is attached to a valid semester that belongs to the same account.

All JMU-specific rules (program types, same-area groups, excluded combinations, ...)
live in valid_account.yaml next to this file. Override the path with the
DUKE_PLANNER_VALID_ACCOUNT_YAML setting.

Usage:
    from account.validation import validate_account, is_account_valid
    issues = validate_account(request.user)
    if not is_account_valid(request.user): redirect("onboarding")

After editing the YAML, run check_config() (e.g. in a test) to catch unknown slugs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from functools import lru_cache
from pathlib import Path

import yaml
from django.conf import settings
from django.core.exceptions import ValidationError


# --------------------------------------------------------------------------- #
# Result types
# --------------------------------------------------------------------------- #

class Severity(str, Enum):
    ERROR = 'error'      # account is not valid / not set up
    WARNING = 'warning'  # valid, but the student should finish or double-check something
    INFO = 'info'        # planning facts worth surfacing (e.g. 150-credit double degree)


@dataclass(frozen=True)
class Issue:
    code: str
    message: str
    severity: Severity = Severity.ERROR
    obj: object | None = None  # the offending StudentProgram / Semester / Class, if any


class ProgramKind(str, Enum):
    DEGREE = 'degree'
    MINOR = 'minor'
    CERTIFICATE = 'certificate'
    OTHER = 'other'  # preparation programs, ROTC, studies abroad, ...


# --------------------------------------------------------------------------- #
# Configuration (loaded from YAML)
# --------------------------------------------------------------------------- #

DEFAULT_CONFIG_PATH = Path(__file__).parent / 'data' / 'valid_account.yaml'


@dataclass(frozen=True)
class Exclusion:
    a: frozenset[str]
    b: frozenset[str]
    message: str
    source: str = ''


@dataclass(frozen=True)
class Requirement:
    program: str
    any_of: frozenset[str]
    severity: Severity
    message: str
    source: str = ''


@dataclass(frozen=True)
class TrackRule:
    program: str
    track_pattern: re.Pattern
    forbidden_with: frozenset[str]
    requires_any_of: frozenset[str]
    message: str
    source: str = ''


@dataclass(frozen=True)
class Config:
    min_semester_year: int = 1908
    max_years_ahead: int = 10
    require_at_least_one_class: bool = False
    program_types: dict[str, tuple[ProgramKind, str]] = field(default_factory=dict)
    program_overrides: dict[str, tuple[ProgramKind | None, str | None]] = field(default_factory=dict)
    same_area: tuple[frozenset[str], ...] = ()
    same_major: tuple[frozenset[str], ...] = ()
    excluded: tuple[Exclusion, ...] = ()
    requires: tuple[Requirement, ...] = ()
    track_rules: tuple[TrackRule, ...] = ()

    def all_slugs(self) -> set[str]:
        """Every program slug the config refers to (for check_config)."""
        slugs = set(self.program_overrides)
        for group in (*self.same_area, *self.same_major):
            slugs |= group
        for e in self.excluded:
            slugs |= e.a | e.b
        for r in self.requires:
            slugs |= {r.program} | r.any_of
        for t in self.track_rules:
            slugs |= {t.program} | t.forbidden_with | t.requires_any_of
        return slugs


def parse_config(raw: dict) -> Config:
    raw = raw or {}
    sem = raw.get('semesters') or {}
    cls = raw.get('classes') or {}

    program_types = {
        str(t): (ProgramKind(v['kind']), v.get('degree') or '')
        for t, v in (raw.get('program_types') or {}).items()
    }
    program_overrides = {
        slug: (ProgramKind(v['kind']) if v.get('kind') else None, v.get('degree'))
        for slug, v in (raw.get('program_overrides') or {}).items()
    }

    def groups(key):
        return tuple(frozenset(g) for g in (raw.get(key) or []))

    excluded = tuple(
        Exclusion(frozenset(e['a']), frozenset(e['b']), e['message'], e.get('source', ''))
        for e in raw.get('excluded_combinations') or []
    )
    requires = tuple(
        Requirement(
            r['program'], frozenset(r['any_of']),
            Severity(r.get('severity', 'error')), r['message'], r.get('source', ''),
        )
        for r in raw.get('requires') or []
    )
    track_rules = tuple(
        TrackRule(
            t['program'], re.compile(t['track_pattern'], re.IGNORECASE),
            frozenset(t.get('forbidden_with') or []),
            frozenset(t.get('requires_any_of') or []),
            t['message'], t.get('source', ''),
        )
        for t in raw.get('track_rules') or []
    )

    return Config(
        min_semester_year=int(sem.get('min_year', 1908)),
        max_years_ahead=int(sem.get('max_years_ahead', 10)),
        require_at_least_one_class=bool(cls.get('require_at_least_one', False)),
        program_types=program_types,
        program_overrides=program_overrides,
        same_area=groups('same_area'),
        same_major=groups('same_major'),
        excluded=excluded,
        requires=requires,
        track_rules=track_rules,
    )


def load_config(path: str | Path | None = None) -> Config:
    path = Path(path or getattr(settings, 'DUKE_PLANNER_VALID_ACCOUNT_YAML', DEFAULT_CONFIG_PATH))
    with open(path, encoding='utf-8') as f:
        return parse_config(yaml.safe_load(f))


@lru_cache(maxsize=1)
def get_config() -> Config:
    """Cached config. Call get_config.cache_clear() after changing the YAML at runtime."""
    return load_config()


def check_config(config: Config | None = None) -> list[str]:
    """Return config slugs that don't exist in the Program table (empty list = OK)."""
    from catalog.models import Program

    config = config or get_config()
    known = set(Program.objects.values_list('slug', flat=True))
    return sorted(config.all_slugs() - known)


# --------------------------------------------------------------------------- #
# Program classification
# --------------------------------------------------------------------------- #

# Fallback for types missing from the YAML: "b.s.", "m.ed.", "ph.d.", "au.d." ...
_DEGREE_ABBR_RE = re.compile(r'(?:[a-z]{1,4}\.)+[a-z]{0,4}')


def _guess_type(type_: str) -> tuple[ProgramKind, str]:
    t = type_.strip().lower()
    if 'minor' in t:
        return ProgramKind.MINOR, ''
    if 'certificate' in t:
        return ProgramKind.CERTIFICATE, ''
    if _DEGREE_ABBR_RE.fullmatch(t.replace(' ', '')) or t.startswith(('bachelor', 'master', 'doctor')):
        return ProgramKind.DEGREE, type_.strip()
    return ProgramKind.OTHER, ''


def classify(program, config: Config) -> tuple[ProgramKind, str]:
    """(kind, normalized degree) for a Program, applying overrides from the YAML."""
    kind, degree = config.program_types.get(program.type or '') or _guess_type(program.type or '')
    o_kind, o_degree = config.program_overrides.get(program.slug, (None, None))
    return (o_kind or kind, o_degree or degree)


# --------------------------------------------------------------------------- #
# Checks (pure functions, easy to unit test)
# --------------------------------------------------------------------------- #

def check_programs(student_programs, programs_with_tracks: set[str], config: Config) -> list[Issue]:
    """Validate a student's program combination.

    student_programs: StudentProgram-like objects with .program (and .track / .track_id).
    programs_with_tracks: slugs of programs that have at least one Track.
    """
    issues: list[Issue] = []
    sps = list(student_programs)
    info = {sp.program.slug: classify(sp.program, config) for sp in sps}
    by_slug = {sp.program.slug: sp for sp in sps}
    slugs = set(by_slug)

    def kind(sp):
        return info[sp.program.slug][0]

    degrees = [sp for sp in sps if kind(sp) == ProgramKind.DEGREE]
    secondary = [sp for sp in sps if kind(sp) in (ProgramKind.MINOR, ProgramKind.CERTIFICATE)]
    degree_slugs = {sp.program.slug for sp in degrees}

    # 1. Must pursue at least one degree.
    if not degrees:
        issues.append(Issue('no_degree', 'Add at least one degree program (a major).'))

    by_level = {
        level: [sp for sp in degrees if sp.program.level == level]
        for level in ('undergraduate', 'graduate')
    }

    # 2a. Minors / certificates need a degree at the same level.
    for sp in secondary:
        if degrees and not by_level.get(sp.program.level):
            issues.append(Issue(
                'orphan_secondary_program',
                f'{sp.program.name} is {sp.program.level}, but you have no '
                f'{sp.program.level} degree program to attach it to.',
                obj=sp,
            ))

    # 2b. At most one degree per same-major group.
    for group in config.same_major:
        hits = sorted(group & degree_slugs)
        if len(hits) > 1:
            names = ' and '.join(by_slug[s].program.name for s in hits)
            issues.append(Issue(
                'duplicate_major',
                f'{names} are the same major; a double major or double degree needs '
                'two different majors.',
                obj=by_slug[hits[-1]],
            ))

    # 2c. No minor in the same area as a degree.
    for group in config.same_area:
        area_degrees = sorted(group & degree_slugs)
        if not area_degrees:
            continue
        for s in sorted(group & slugs):
            if info[s][0] == ProgramKind.MINOR:
                issues.append(Issue(
                    'major_minor_same_area',
                    f"You can't minor in {by_slug[s].program.name} while majoring in "
                    f'{by_slug[area_degrees[0]].program.name} (same area).',
                    obj=by_slug[s],
                ))

    # 2d. Explicit exclusions.
    for rule in config.excluded:
        a_hits, b_hits = sorted(rule.a & slugs), sorted(rule.b & slugs)
        if any(x != y for x in a_hits for y in b_hits):
            issues.append(Issue('excluded_combination', rule.message, obj=by_slug[b_hits[0]]))

    # 2e. Programs that require another program.
    for rule in config.requires:
        if rule.program in slugs and not (rule.any_of & slugs):
            issues.append(Issue(
                'missing_required_program', rule.message,
                severity=rule.severity, obj=by_slug[rule.program],
            ))

    # 2f. Tracks / concentrations.
    for sp in sps:
        if sp.track_id is not None and sp.track.program_id != sp.program.slug:
            issues.append(Issue(
                'track_wrong_program',
                f"Concentration '{sp.track.name}' doesn't belong to {sp.program.name}.",
                obj=sp,
            ))
            continue
        if sp.track_id is None:
            if sp.program.slug in programs_with_tracks:
                issues.append(Issue(
                    'track_not_chosen',
                    f'Choose a concentration for {sp.program.name}.',
                    severity=Severity.WARNING,
                    obj=sp,
                ))
            continue
        for rule in config.track_rules:
            if rule.program != sp.program.slug or not rule.track_pattern.search(sp.track.name):
                continue
            forbidden = rule.forbidden_with & slugs
            missing = rule.requires_any_of and not (rule.requires_any_of & slugs)
            if forbidden or missing:
                issues.append(Issue('track_not_open', rule.message, obj=sp))

    # 2g. Planning facts.
    ug_degrees = {info[sp.program.slug][1] for sp in by_level['undergraduate']}
    if len(ug_degrees) >= 2:
        issues.append(Issue(
            'double_degree',
            'Your majors span different degree types, so this is a double degree: '
            'JMU requires at least 150 credits, including 60 at JMU.',
            severity=Severity.INFO,
        ))
    elif len(by_level['undergraduate']) >= 2:
        issues.append(Issue(
            'double_major',
            'Double major (one degree): at least 120 credits, and you must meet the '
            'degree requirements once plus every requirement of both majors.',
            severity=Severity.INFO,
        ))

    if by_level['undergraduate'] and by_level['graduate']:
        issues.append(Issue(
            'mixed_levels',
            "You have both undergraduate and graduate degree programs. That's only "
            "possible through a combined/accelerated program; double-check it's right.",
            severity=Severity.WARNING,
        ))

    return issues


def check_semesters(
    semesters, config: Config, today: date | None = None, occupied: set[int] | None = None
) -> list[Issue]:
    """occupied: pks of the semesters that have classes; if given, the others
    are flagged as empty."""
    today = today or date.today()
    max_year = today.year + config.max_years_ahead
    issues: list[Issue] = []
    sems = list(semesters)

    if not sems:
        issues.append(Issue('no_semester', 'Add at least one semester.'))

    for s in sems:
        if not (config.min_semester_year <= s.year <= max_year):
            issues.append(Issue(
                'semester_year_out_of_range',
                f'{s}: year must be between {config.min_semester_year} and {max_year}.',
                obj=s,
            ))
        if occupied is not None and s.pk not in occupied:
            issues.append(Issue('empty_semester', f'{s} has no classes.', obj=s))
    return issues


def check_classes(account_id, classes, config: Config) -> list[Issue]:
    issues: list[Issue] = []
    cls = list(classes)

    if config.require_at_least_one_class and not cls:
        issues.append(Issue('no_classes', 'Add at least one class.'))

    seen: set[tuple[int, object]] = set()
    for c in cls:
        if c.semester_id is None:
            issues.append(Issue(
                'class_without_semester',
                f"{c.course.code} isn't assigned to a semester.",
                obj=c,
            ))
            continue
        if c.semester.account_id != account_id:
            issues.append(Issue(
                'class_semester_other_account',
                f"{c.course.code} is assigned to a semester that isn't yours.",
                obj=c,
            ))
        if not (c.course.min_credits <= c.credits <= c.course.max_credits):
            cr = 'credit' if c.credits == 1 else 'credits'
            official_range = (
                f'{c.course.min_credits}-{c.course.max_credits} credits'
                if c.course.credits is None else f'{c.course.credits} credit'
                if c.course.credits == 1 else f'{c.course.credits} credits'
            )
            issues.append(Issue(
                'class_credits_out_of_range',
                f'{c.course.code} in {c.semester} is {c.credits} {cr}, outside the '
                f'official {official_range} in the JMU catalog.',
                obj=c,
                severity=Severity.WARNING
            ))
        key = (c.semester_id, c.course_id)
        if key in seen:
            issues.append(Issue(
                'duplicate_class_in_semester',
                f'{c.course.code} is listed twice in {c.semester}.',
                obj=c,
            ))
        seen.add(key)
    return issues


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #

def validate_account(account, today: date | None = None, config: Config | None = None) -> list[Issue]:
    """Return every issue for the account (errors, warnings and info)."""
    from catalog.models import Track  # local import avoids app-loading cycles

    config = config or get_config()
    student_programs = list(account.programs.select_related('program', 'track'))
    slugs = [sp.program_id for sp in student_programs]
    programs_with_tracks = set(
        Track.objects.filter(program_id__in=slugs)
        .values_list('program_id', flat=True)
        .distinct()
    )

    issues: list[Issue] = []
    issues += check_programs(student_programs, programs_with_tracks, config)
    classes = list(account.classes.select_related('semester', 'course'))
    occupied = {c.semester_id for c in classes}
    issues += check_semesters(account.semesters.all(), config, today=today, occupied=occupied)
    issues += check_classes(account.pk, classes, config)
    return issues


def errors(issues: list[Issue]) -> list[Issue]:
    return [i for i in issues if i.severity == Severity.ERROR]


def is_account_valid(account) -> bool:
    return not errors(validate_account(account))


def assert_account_valid(account) -> None:
    """Raise a Django ValidationError listing every error (for forms / DRF / admin)."""
    errs = errors(validate_account(account))
    if errs:
        raise ValidationError([ValidationError(e.message, code=e.code) for e in errs])
