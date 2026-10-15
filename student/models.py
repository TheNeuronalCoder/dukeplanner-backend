from django.db import models


class Curriculum(models.Model):
    account = models.ForeignKey('account.Account', on_delete=models.CASCADE, related_name='programs')
    program = models.ForeignKey('catalog.Program', on_delete=models.PROTECT, related_name='account_programs')
    # Null when the program has no tracks or one hasn't been chosen yet.
    track = models.ForeignKey(
        'catalog.Track', on_delete=models.PROTECT, null=True, blank=True, related_name='account_programs'
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['account', 'program'], name='unique_program_per_account'),
        ]

    def __str__(self):
        return f'{self.account} - {self.program}'


class Semester(models.Model):
    # Integer values so semesters within a year sort chronologically.
    class Term(models.IntegerChoices):
        SPRING = 1, 'Spring'
        SUMMER = 2, 'Summer'
        FALL = 3, 'Fall'
        WINTER = 4, 'Winter'

    account = models.ForeignKey('account.Account', on_delete=models.CASCADE, related_name='semesters')
    term = models.PositiveSmallIntegerField(choices=Term.choices)
    year = models.PositiveSmallIntegerField()
    # Credits attempted/earned are computed from the semester's classes.

    class Meta:
        ordering = ['account', 'year', 'term']
        constraints = [
            models.UniqueConstraint(fields=['account', 'term', 'year'], name='unique_semester_per_account'),
        ]

    def __str__(self):
        return f'{self.get_term_display()} {self.year}'


class Class(models.Model):
    account = models.ForeignKey('account.Account', on_delete=models.CASCADE, related_name='classes')
    course = models.ForeignKey('catalog.Course', on_delete=models.PROTECT, related_name='classes')
    # Null until the class is placed in a semester (validation flags it as an error).
    semester = models.ForeignKey(
        Semester, on_delete=models.CASCADE, null=True, blank=True, related_name='classes'
    )
    # Within the course's min_credits..max_credits (variable-credit courses vary per student).
    credits = models.PositiveSmallIntegerField()
    # False when the class was failed, so its credits are attempted but not earned.
    credits_earned = models.BooleanField(default=True)

    class Meta:
        verbose_name_plural = 'classes'

    def __str__(self):
        return f'{self.account} - {self.course.code}'
