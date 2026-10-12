from django.core import mail
from django.test import TestCase

from .emails import send_otp_email


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
