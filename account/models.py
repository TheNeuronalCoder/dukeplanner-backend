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


class VerificationCode(models.Model):
    # Keyed by eID rather than Account so codes can be sent before the account exists.
    username = models.CharField(max_length=150, unique=True)
    code_hash = models.CharField(max_length=128)
    created_at = models.DateTimeField()
    attempts = models.PositiveSmallIntegerField(default=0)

    def __str__(self):
        return self.username
