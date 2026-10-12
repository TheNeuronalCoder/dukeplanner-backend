from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string

OTP_EXPIRY_MINUTES = 10


def send_otp_email(to_email, code):
    context = {'code': code, 'expiry_minutes': OTP_EXPIRY_MINUTES}
    message = EmailMultiAlternatives(
        subject='Verify your email address',
        body=render_to_string('account/emails/otp_code.txt', context),
        to=[to_email],
    )
    message.attach_alternative(render_to_string('account/emails/otp_code.html', context), 'text/html')
    message.send()
