from django.contrib.auth.models import AbstractBaseUser, BaseUserManager
from django.db import models


class AccountManager(BaseUserManager):
    def create_user(self, username, first_name, last_name, **extra_fields):
        if not username:
            raise ValueError('Accounts must have a username')

        account = self.model(
            username=username,
            first_name=first_name,
            last_name=last_name,
            **extra_fields,
        )
        account.save(using=self._db)
        return account


class Account(AbstractBaseUser):
    password = None

    username = models.CharField(max_length=150, unique=True)
    first_name = models.CharField(max_length=150)
    last_name = models.CharField(max_length=150)

    objects = AccountManager()

    USERNAME_FIELD = 'username'
    REQUIRED_FIELDS = ['first_name', 'last_name']

    @property
    def email(self):
        return f'{self.username}@dukes.jmu.edu'

    def __str__(self):
        return self.username


class Semester(models.Model):
    # Integer values so semesters within a year sort chronologically.
    class Term(models.IntegerChoices):
        SPRING = 1, 'Spring'
        SUMMER = 2, 'Summer'
        FALL = 3, 'Fall'
        WINTER = 4, 'Winter'

    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name='semesters')
    term = models.PositiveSmallIntegerField(choices=Term.choices)
    year = models.PositiveSmallIntegerField()
    credits_attempted = models.PositiveSmallIntegerField(default=0)
    credits_earned = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ['account', 'year', 'term']
        constraints = [
            models.UniqueConstraint(fields=['account', 'term', 'year'], name='unique_semester_per_account'),
        ]

    def __str__(self):
        return f'{self.get_term_display()} {self.year}'


class Class(models.Model):
    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name='classes')
    course = models.ForeignKey('catalog.Course', on_delete=models.PROTECT, related_name='classes')

    class Meta:
        verbose_name_plural = 'classes'

    def __str__(self):
        return f'{self.account} - {self.course.code}'


class StudentProgram(models.Model):
    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name='programs')
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
