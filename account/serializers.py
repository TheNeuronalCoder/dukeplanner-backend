from rest_framework import serializers

from catalog.models import Course, Program
from student.models import Semester


class CourseField(serializers.CharField):
    """A course code like "CS 149", resolved to a Course."""

    def to_internal_value(self, data):
        prefix, _, number = super().to_internal_value(data).upper().partition(' ')
        course = Course.objects.filter(prefix_id=prefix, number=number.strip()).first()
        if course is None:
            raise serializers.ValidationError(f'Unknown course {data!r}.')
        return course


class SemesterSerializer(serializers.Serializer):
    term = serializers.ChoiceField(choices=[t.lower() for t in Semester.Term.names])
    year = serializers.IntegerField()
    courses = serializers.ListField(child=CourseField())


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
