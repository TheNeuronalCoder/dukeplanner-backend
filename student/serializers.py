from rest_framework import serializers

from account.serializers import ClassEntryField, ClassSerializer, CourseField, credits_field
from catalog.models import Program, Track
from catalog.utils.courses import course_credits

from .models import Class, Semester

TERMS = ['fall', 'winter', 'spring', 'summer']


class OwnedSemesterField(serializers.PrimaryKeyRelatedField):
    """A Semester pk, limited to the requesting account's semesters."""

    def get_queryset(self):
        return Semester.objects.filter(account=self.context['request'].user)


class OwnedClassField(serializers.PrimaryKeyRelatedField):
    """A Class pk, limited to the requesting account's classes."""

    def get_queryset(self):
        return Class.objects.filter(account=self.context['request'].user).select_related('course')


class AddCourseSerializer(ClassSerializer):
    semester = OwnedSemesterField()


class UpdateCourseSerializer(serializers.Serializer):
    """The class to update and the fields to change. Changing the course without
    giving `credits` resets them to the new course's credits."""

    id = OwnedClassField()
    course = CourseField(required=False)
    semester = OwnedSemesterField(required=False)
    credits = credits_field()
    credits_earned = serializers.BooleanField(required=False)

    def validate(self, data):
        if 'course' in data:
            data.setdefault('credits', course_credits(data['course']))
        return data


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
    term = serializers.ChoiceField(choices=TERMS)
    year = serializers.IntegerField(min_value=0)
    # Semesters can't be empty, so each is added with its classes.
    courses = serializers.ListField(child=ClassEntryField(), allow_empty=False)

    def validate_courses(self, courses):
        codes = [c['course'].pk for c in courses]
        if len(set(codes)) != len(codes):
            raise serializers.ValidationError('A course may only be listed once per semester.')
        return courses


class AddCoursesSerializer(serializers.ListSerializer):
    def validate(self, items):
        account = self.context['request'].user
        keys = [(i['semester'].pk, i['course'].pk) for i in items]
        existing = set(account.classes.values_list('semester_id', 'course_id'))
        if len(set(keys)) != len(keys) or existing.intersection(keys):
            raise serializers.ValidationError('A course may only be listed once per semester.')
        return items


class UpdateCoursesSerializer(serializers.ListSerializer):
    # A course listed twice in a semester after the update is caught by
    # account validation.
    def validate(self, items):
        ids = [i['id'].pk for i in items]
        if len(set(ids)) != len(ids):
            raise serializers.ValidationError('Each course may only be listed once.')
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


def list_serializer(child, list_class, data, request):
    return list_class(child=child(), data=data, context={'request': request}, allow_empty=False)
