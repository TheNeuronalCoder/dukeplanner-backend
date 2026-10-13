from rest_framework import serializers

from account.serializers import CourseField
from catalog.models import Program, Track

from .models import Semester


class OwnedSemesterField(serializers.PrimaryKeyRelatedField):
    """A Semester pk, limited to the requesting account's semesters."""

    def get_queryset(self):
        return Semester.objects.filter(account=self.context['request'].user)


class AddCourseSerializer(serializers.Serializer):
    course = CourseField()
    # Omit or null to leave the class unassigned.
    semester = OwnedSemesterField(required=False, allow_null=True, default=None)


class AddProgramSerializer(serializers.Serializer):
    program = serializers.SlugRelatedField(slug_field='slug', queryset=Program.objects.all())
    track = serializers.PrimaryKeyRelatedField(
        queryset=Track.objects.all(), required=False, allow_null=True, default=None
    )

    def validate(self, data):
        track = data['track']
        if track is not None and track.program_id != data['program'].slug:
            raise serializers.ValidationError(
                {'track': f"'{track.name}' doesn't belong to {data['program'].name}"}
            )
        return data


class AddSemesterSerializer(serializers.Serializer):
    term = serializers.ChoiceField(choices=[t.lower() for t in Semester.Term.names])
    year = serializers.IntegerField(min_value=0)
    credits_attempted = serializers.IntegerField(min_value=0, default=0)
    credits_earned = serializers.IntegerField(min_value=0, default=0)


class AddCoursesSerializer(serializers.ListSerializer):
    def validate(self, items):
        account = self.context['request'].user
        keys = [(i['semester'] and i['semester'].pk, i['course'].pk) for i in items]
        existing = set(
            account.classes.filter(semester__isnull=False).values_list('semester_id', 'course_id')
        )
        if len(set(keys)) != len(keys) or any(k in existing for k in keys if k[0] is not None):
            raise serializers.ValidationError('A course may only be listed once per semester.')
        return items


class AddProgramsSerializer(serializers.ListSerializer):
    def validate(self, items):
        account = self.context['request'].user
        slugs = [i['program'].slug for i in items]
        if len(set(slugs)) != len(slugs) or account.programs.filter(program_id__in=slugs).exists():
            raise serializers.ValidationError('Each program may only be added once.')
        return items


class AddSemestersSerializer(serializers.ListSerializer):
    def validate(self, items):
        account = self.context['request'].user
        keys = [(Semester.Term[i['term'].upper()], i['year']) for i in items]
        existing = set(account.semesters.values_list('term', 'year'))
        if len(set(keys)) != len(keys) or existing.intersection(keys):
            raise serializers.ValidationError('Each semester may only be listed once.')
        return items


def add_serializer(child, list_class, data, request):
    return list_class(child=child(), data=data, context={'request': request}, allow_empty=False)
