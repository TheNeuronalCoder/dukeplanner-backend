import re
import secrets
from datetime import timedelta

from django.db.models import F
from django.http import JsonResponse
from django.utils import timezone
from django.utils.crypto import constant_time_compare, salted_hmac
from rest_framework import status
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework_simplejwt.tokens import RefreshToken

from .emails import OTP_EXPIRY_MINUTES, send_otp_email
from .models import Account, VerificationCode

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
