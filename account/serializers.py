from rest_framework import serializers

from catalog.models import Course, Program
from catalog.utils.courses import course_credits
from student.models import MAX_CLASS_CREDITS, MIN_CLASS_CREDITS, Semester


class CourseField(serializers.CharField):
    """A course code like "CS 149", resolved to a Course."""

    def to_internal_value(self, data):
        prefix, _, number = super().to_internal_value(data).upper().partition(' ')
        course = Course.objects.filter(prefix_id=prefix, number=number.strip()).first()
        if course is None:
            raise serializers.ValidationError(f'Unknown course {data!r}.')
        return course


def credits_field():
    """The credits a class was taken for: 1-9, but not checked against the catalog,
    since a course's credits can change after a student took it."""
    message = f'Credits must be between {MIN_CLASS_CREDITS} and {MAX_CLASS_CREDITS}.'
    return serializers.IntegerField(
        required=False, min_value=MIN_CLASS_CREDITS, max_value=MAX_CLASS_CREDITS,
        error_messages={'min_value': message, 'max_value': message},
    )


class ClassSerializer(serializers.Serializer):
    """A class taken: the course, the credits taken for it (defaults to the course's
    credits; see credits_field) and whether they were earned (passed)."""

    course = CourseField()
    credits = credits_field()
    credits_earned = serializers.BooleanField(default=True)

    def validate(self, data):
        data.setdefault('credits', course_credits(data['course']))
        return data


class ClassEntryField(serializers.Field):
    """A ClassSerializer object, or a bare course code (the older format) for a
    class with the course's default credits that was passed."""

    def to_internal_value(self, data):
        serializer = ClassSerializer(data={'course': data} if isinstance(data, str) else data)
        serializer.is_valid(raise_exception=True)
        return serializer.validated_data


class SemesterSerializer(serializers.Serializer):
    term = serializers.ChoiceField(choices=[t.lower() for t in Semester.Term.names])
    year = serializers.IntegerField()
    courses = serializers.ListField(child=ClassEntryField(), allow_empty=False)


class SetupSerializer(serializers.Serializer):
    first_name = serializers.CharField(max_length=150)
    last_name = serializers.CharField(max_length=150)
    programs = serializers.SlugRelatedField(many=True, slug_field='slug', queryset=Program.objects.all())
    semesters = SemesterSerializer(many=True)

    def validate_programs(self, programs):
        if len(set(programs)) != len(programs):
            raise serializers.ValidationError('Programs must be unique.')
        return programs

    def validate_semesters(self, semesters):
        keys = [(s['term'], s['year']) for s in semesters]
        if len(set(keys)) != len(keys):
            raise serializers.ValidationError('Each semester may only be listed once.')
        return semesters
