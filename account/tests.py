import re
from datetime import timedelta

from django.core import mail
from django.test import TestCase
from rest_framework_simplejwt.tokens import AccessToken

from .emails import OTP_EXPIRY_MINUTES, send_otp_email
from .models import Account, VerificationCode
from .views import MAX_ATTEMPTS


class OtpEmailTests(TestCase):
    def test_send_otp_email(self):
        send_otp_email('abc123@dukes.jmu.edu', '150379')

        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, ['abc123@dukes.jmu.edu'])
        self.assertEqual(message.subject, 'Verify your email address')
        self.assertIn('150379', message.body)

        html, mimetype = message.alternatives[0]
        self.assertEqual(mimetype, 'text/html')
        self.assertIn('150379', html)
        self.assertIn('https://dukeaiplanner.web.app/android-chrome-192x192.png', html)


class VerificationCodeTests(TestCase):
    def request_code(self, eid='abc'):
        return self.client.post('/account/request-code/', {'eid': eid}, content_type='application/json')

    def verify_code(self, code, eid='abc'):
        return self.client.post(
            '/account/verify-code/', {'eid': eid, 'code': code}, content_type='application/json'
        )

    def sent_code(self):
        return re.search(r'\b\d{6}\b', mail.outbox[-1].body).group()

    def test_request_code_sends_email(self):
        response = self.request_code()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(mail.outbox[0].to, ['abc@dukes.jmu.edu'])
        self.assertTrue(VerificationCode.objects.filter(username='abc').exists())

    def test_request_code_rejects_invalid_eid(self):
        for eid in ['', 'ABC', 'abc123', 'abc@dukes.jmu.edu', ' abc']:
            self.assertEqual(self.request_code(eid).status_code, 400, eid)
        self.assertEqual(len(mail.outbox), 0)

    def test_request_code_cooldown(self):
        self.request_code()
        response = self.request_code()

        self.assertEqual(response.status_code, 429)
        self.assertIn('Retry-After', response.headers)
        self.assertEqual(len(mail.outbox), 1)

    def test_verify_code_creates_account_and_returns_jwt(self):
        self.request_code()
        response = self.verify_code(self.sent_code())

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['created'])
        account = Account.objects.get(username='abc')
        self.assertEqual(str(AccessToken(response.json()['access'])['user_id']), str(account.pk))
        self.assertFalse(VerificationCode.objects.exists())

    def test_verify_code_existing_account(self):
        Account.objects.create_user('abc', 'Jane', 'Doe')
        self.request_code()
        response = self.verify_code(self.sent_code())

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()['created'])

    def test_code_is_single_use(self):
        self.request_code()
        code = self.sent_code()
        self.verify_code(code)

        self.assertEqual(self.verify_code(code).status_code, 400)

    def test_wrong_code(self):
        self.request_code()
        code = self.sent_code()
        wrong = '000000' if code != '000000' else '111111'

        self.assertEqual(self.verify_code(wrong).status_code, 400)
        self.assertEqual(VerificationCode.objects.get().attempts, 1)
        self.assertEqual(self.verify_code(code).status_code, 200)

    def test_too_many_attempts_invalidates_code(self):
        self.request_code()
        code = self.sent_code()
        wrong = '000000' if code != '000000' else '111111'
        for _ in range(MAX_ATTEMPTS):
            self.verify_code(wrong)

        self.assertEqual(self.verify_code(code).status_code, 400)
        self.assertFalse(VerificationCode.objects.exists())

    def test_expired_code(self):
        self.request_code()
        pending = VerificationCode.objects.get()
        pending.created_at -= timedelta(minutes=OTP_EXPIRY_MINUTES, seconds=1)
        pending.save()

        self.assertEqual(self.verify_code(self.sent_code()).status_code, 400)

    def test_verify_code_rejects_malformed_input(self):
        self.assertEqual(self.verify_code('12345').status_code, 400)
        self.assertEqual(self.verify_code(123456).status_code, 400)
        self.assertEqual(self.verify_code('123456', eid='abc1').status_code, 400)
