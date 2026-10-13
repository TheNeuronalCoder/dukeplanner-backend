from rest_framework import serializers

from catalog.models import Course, Program
from catalog.utils.courses import course_credits, credits_range_error
from student.models import Semester


class CourseField(serializers.CharField):
    """A course code like "CS 149", resolved to a Course."""

    def to_internal_value(self, data):
        prefix, _, number = super().to_internal_value(data).upper().partition(' ')
        course = Course.objects.filter(prefix_id=prefix, number=number.strip()).first()
        if course is None:
            raise serializers.ValidationError(f'Unknown course {data!r}.')
        return course


class ClassSerializer(serializers.Serializer):
    """A class taken: the course, the credits taken for it (defaults to the course's
    credits, and must be within its range) and whether they were earned (passed)."""

    course = CourseField()
    credits = serializers.IntegerField(required=False, min_value=0)
    credits_earned = serializers.BooleanField(default=True)

    def validate(self, data):
        course = data['course']
        data.setdefault('credits', course_credits(course))
        error = credits_range_error(course, data['credits'])
        if error:
            raise serializers.ValidationError({'credits': error})
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
    courses = serializers.ListField(child=ClassEntryField())


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
