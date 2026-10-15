from rest_framework import serializers

from .models import Program

SEARCH_DEFAULT_LIMIT = 20
SEARCH_MAX_LIMIT = 100


class CommaListField(serializers.ListField):
    """A comma-separated and/or repeated query parameter, deduplicated."""

    child = serializers.CharField()

    def __init__(self, **kwargs):
        super().__init__(required=False, default=list, **kwargs)

    def to_internal_value(self, data):
        parts = (part.strip() for value in data for part in value.split(','))
        return list(dict.fromkeys(part for part in parts if part))


class SearchSerializer(serializers.Serializer):
    q = serializers.CharField()
    limit = serializers.IntegerField(min_value=1, default=SEARCH_DEFAULT_LIMIT)

    def validate_q(self, value):
        return ' '.join(value.split())

    def validate_limit(self, value):
        return min(value, SEARCH_MAX_LIMIT)


class CourseSearchSerializer(SearchSerializer):
    subject = CommaListField()

    def validate_subject(self, value):
        return list(dict.fromkeys(s.upper() for s in value))


class CoursesSerializer(serializers.Serializer):
    codes = CommaListField()


class ProgramSearchSerializer(SearchSerializer):
    level = serializers.ChoiceField(choices=Program.Level.values, required=False)
    type = CommaListField()
