import re
import secrets
from datetime import timedelta

from django.db import transaction
from django.db.models import F
from django.http import JsonResponse
from django.utils import timezone
from django.utils.crypto import constant_time_compare, salted_hmac
from rest_framework import status
from rest_framework.decorators import api_view, authentication_classes, parser_classes, permission_classes
from rest_framework.parsers import JSONParser
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken, OutstandingToken
from rest_framework_simplejwt.tokens import RefreshToken

from student.models import Class, Curriculum, Semester

from .emails import OTP_EXPIRY_MINUTES, send_otp_email
from .models import Account, VerificationCode
from .serializers import SetupSerializer
from .validation import errors, validate_account

CODE_LENGTH = 6
MAX_ATTEMPTS = 5
RESEND_COOLDOWN = timedelta(seconds=60)

EID_RE = re.compile(r'[a-z]+')
CODE_RE = re.compile(rf'\d{{{CODE_LENGTH}}}')


def _hash_code(eid, code):
    return salted_hmac('account.VerificationCode', f'{eid}:{code}').hexdigest()


def _get_eid(request):
    eid = request.data.get('eid')
    return eid if isinstance(eid, str) and EID_RE.fullmatch(eid) else None


@api_view(['POST'])
@authentication_classes([])
@permission_classes([AllowAny])
def request_code(request):
    """POST /auth/request-code/ {"eid": "abc"}

    Emails a one-time code to <eid>@dukes.jmu.edu. Requesting a new code
    replaces the previous one; requests are limited to one per minute per eID.
    """
    eid = _get_eid(request)
    if eid is None:
        return JsonResponse({'error': "Missing or invalid 'eid'."}, status=status.HTTP_400_BAD_REQUEST)

    now = timezone.now()
    existing = VerificationCode.objects.filter(username=eid).first()
    if existing and now - existing.created_at < RESEND_COOLDOWN:
        retry_after = int((existing.created_at + RESEND_COOLDOWN - now).total_seconds()) + 1
        return JsonResponse(
            {'error': 'A code was sent recently. Try again shortly.', 'retry_after': retry_after},
            status=status.HTTP_429_TOO_MANY_REQUESTS,
            headers={'Retry-After': str(retry_after)},
        )

    code = f'{secrets.randbelow(10 ** CODE_LENGTH):0{CODE_LENGTH}d}'
    VerificationCode.objects.update_or_create(
        username=eid,
        defaults={'code_hash': _hash_code(eid, code), 'created_at': now, 'attempts': 0},
    )
    send_otp_email(f'{eid}@dukes.jmu.edu', code)

    return JsonResponse({'detail': 'Verification code sent.', 'expires_in': OTP_EXPIRY_MINUTES * 60})


@api_view(['POST'])
@authentication_classes([])
@permission_classes([AllowAny])
def verify_code(request):
    """POST /auth/verify-code/ {"eid": "abc", "code": "123456"}

    Returns {"access", "refresh", "created"} JWTs if the code is valid, creating
    the account on first sign-in. A code is single use, expires after
    OTP_EXPIRY_MINUTES, and is invalidated after MAX_ATTEMPTS wrong guesses.
    """
    eid = _get_eid(request)
    if eid is None:
        return JsonResponse({'error': "Missing or invalid 'eid'."}, status=status.HTTP_400_BAD_REQUEST)
    code = request.data.get('code')
    if not isinstance(code, str) or not CODE_RE.fullmatch(code):
        return JsonResponse(
            {'error': f"'code' must be {CODE_LENGTH} digits."}, status=status.HTTP_400_BAD_REQUEST
        )

    invalid = JsonResponse({'error': 'Invalid or expired code.'}, status=status.HTTP_400_BAD_REQUEST)
    pending = VerificationCode.objects.filter(username=eid).first()
    if pending is None:
        return invalid
    if (
        pending.attempts >= MAX_ATTEMPTS
        or timezone.now() - pending.created_at > timedelta(minutes=OTP_EXPIRY_MINUTES)
    ):
        pending.delete()
        return invalid
    if not constant_time_compare(pending.code_hash, _hash_code(eid, code)):
        VerificationCode.objects.filter(pk=pending.pk).update(attempts=F('attempts') + 1)
        return invalid

    # Delete by hash so two concurrent requests can't both redeem the same code.
    deleted, _ = VerificationCode.objects.filter(pk=pending.pk, code_hash=pending.code_hash).delete()
    if not deleted:
        return invalid

    # Names are filled in during onboarding.
    account, _ = Account.objects.get_or_create(
        username=eid, defaults={'first_name': eid[-2], 'last_name': eid[:-2]}
    )
    refresh = RefreshToken.for_user(account)
    return JsonResponse({
        'access': str(refresh.access_token),
        'refresh': str(refresh)
    })


def _issue_json(issue):
    return {'code': issue.code, 'message': issue.message, 'severity': issue.severity.value}


@api_view(['POST'])
@parser_classes([JSONParser])
@permission_classes([IsAuthenticated])
def setup(request):
    """POST /account/setup/ (see SetupSerializer for the body)

    Replaces the signed-in account's names, programs, semesters and classes, then
    runs account.validation. On ERROR-level issues nothing is saved and they are
    returned with a 400; otherwise any warnings/info come back with the success.

    Sample request body:
    {
        "first_name": "Menelik",
        "last_name": "Eyasu",
        "programs": ["computer-science-bs", "mathematics-minor"],
        "semesters": [
            {
                "term": "fall",
                "year": 2025,
                "courses": [
                    {"course": "CS 149", "credits": 3, "credits_earned": true},
                    {"course": "MATH 235", "credits": 4, "credits_earned": false},
                    {"course": "IND 499", "credits": 2}
                ]
            },
            {
                "term": "spring",
                "year": 2026,
                "courses": ["CS 159", "CS 227", "MATH 236"]
            }
        ]
    }

    Each course is an object with `course`, optional `credits` (defaults to the
    course's credits; must be within its min/max range) and optional
    `credits_earned` (false if failed; defaults to true). A bare course code is
    also accepted, meaning the defaults.
    """
    serializer = SetupSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data

    account = request.user
    with transaction.atomic():
        account.first_name = data['first_name']
        account.last_name = data['last_name']
        account.save(update_fields=['first_name', 'last_name'])

        account.programs.all().delete()
        account.semesters.all().delete()
        account.classes.all().delete()

        Curriculum.objects.bulk_create(Curriculum(account=account, program=p) for p in data['programs'])
        for s in data['semesters']:
            semester = Semester.objects.create(
                account=account, term=Semester.Term[s['term'].upper()], year=s['year']
            )
            Class.objects.bulk_create(
                Class(
                    account=account,
                    semester=semester,
                    course=c['course'],
                    credits=c['credits'],
                    credits_earned=c['credits_earned'],
                )
                for c in s['courses']
            )

        issues = validate_account(account)
        if errors(issues):
            transaction.set_rollback(True)
            return JsonResponse(
                {'error': 'Account setup is invalid.', 'issues': [_issue_json(i) for i in errors(issues)]},
                status=status.HTTP_400_BAD_REQUEST,
            )

    return JsonResponse({'detail': 'Account set up.', 'issues': [_issue_json(i) for i in issues]})


@api_view(['DELETE'])
@permission_classes([IsAuthenticated])
def delete_account(request):
    """DELETE /account/delete/

    Permanently deletes the signed-in account along with its programs, semesters
    and classes. Only the caller's own account can be deleted; the target is never
    taken from the request body. Outstanding refresh tokens are blacklisted first
    so they can't be used after the account is gone.
    """
    account = request.user
    with transaction.atomic():
        # The blacklist app nulls OutstandingToken.user on delete, so revoke while
        # the tokens can still be found by user.
        for token in OutstandingToken.objects.filter(user=account):
            BlacklistedToken.objects.get_or_create(token=token)
        VerificationCode.objects.filter(username=account.username).delete()
        account.delete()

    return JsonResponse({'detail': 'Account deleted.'})
