import re
from datetime import timedelta

from django.core import mail
from django.test import TestCase
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import AccessToken

from catalog.models import Course, Program, Subject

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
        return self.client.post('/auth/request-code/', {'eid': eid}, content_type='application/json')

    def verify_code(self, code, eid='abc'):
        return self.client.post(
            '/auth/verify-code/', {'eid': eid, 'code': code}, content_type='application/json'
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
        account = Account.objects.get(username='abc')
        self.assertEqual(str(AccessToken(response.json()['access'])['user_id']), str(account.pk))
        self.assertFalse(VerificationCode.objects.exists())

    def test_verify_code_existing_account(self):
        Account.objects.create_user('abc', 'Jane', 'Doe')
        self.request_code()
        response = self.verify_code(self.sent_code())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Account.objects.count(), 1)
        self.assertEqual(Account.objects.get().first_name, 'Jane')

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


class SetupTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        for slug, type_ in [('computer-science-bs', 'B.S.'), ('mathematics-minor', 'Minor')]:
            Program.objects.create(
                slug=slug, name=slug, level='undergraduate', type=type_,
                url='https://catalog.jmu.edu/', requirements_pdf_url='https://catalog.jmu.edu/',
            )
        for prefix, number in [('CS', '149'), ('CS', '159'), ('MATH', '235')]:
            subject, _ = Subject.objects.get_or_create(code=prefix, defaults={'name': prefix})
            Course.objects.create(
                prefix=subject, number=number, title=f'{prefix} {number}',
                credits=3, min_credits=3, max_credits=3,
            )
        Course.objects.create(
            prefix=Subject.objects.get(code='CS'), number='499', title='Honors Thesis',
            credits=None, min_credits=1, max_credits=6,
        )

    def setUp(self):
        self.account = Account.objects.create_user('abc', 'a', 'bc')
        self.client.force_authenticate(self.account)

    def body(self, **overrides):
        return {
            'first_name': 'Jane',
            'last_name': 'Doe',
            'programs': ['computer-science-bs', 'mathematics-minor'],
            'semesters': [
                {'term': 'fall', 'year': 2025, 'courses': ['CS 149', 'MATH 235']},
                {'term': 'spring', 'year': 2026, 'courses': ['cs 159']},
            ],
            **overrides,
        }

    def setup(self, body):
        return self.client.post('/account/setup/', body, format='json')

    def test_setup_saves_account(self):
        response = self.setup(self.body())

        self.assertEqual(response.status_code, 200, response.content)
        self.account.refresh_from_db()
        self.assertEqual((self.account.first_name, self.account.last_name), ('Jane', 'Doe'))
        self.assertCountEqual(
            self.account.programs.values_list('program_id', flat=True),
            ['computer-science-bs', 'mathematics-minor'],
        )
        fall = self.account.semesters.get(year=2025)
        self.assertCountEqual([c.course.code for c in fall.classes.all()], ['CS 149', 'MATH 235'])
        self.assertEqual(self.account.classes.count(), 3)

    def test_setup_replaces_previous_setup(self):
        self.setup(self.body())
        response = self.setup(self.body(
            programs=['computer-science-bs'],
            semesters=[{'term': 'summer', 'year': 2026, 'courses': ['CS 149']}],
        ))

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.account.programs.count(), 1)
        self.assertEqual(self.account.semesters.get().get_term_display(), 'Summer')
        self.assertEqual(self.account.classes.count(), 1)

    def test_invalid_account_is_rolled_back(self):
        self.setup(self.body())
        response = self.setup(self.body(first_name='John', programs=['mathematics-minor']))

        self.assertEqual(response.status_code, 400)
        self.assertIn('no_degree', [i['code'] for i in response.json()['issues']])
        self.account.refresh_from_db()
        self.assertEqual(self.account.first_name, 'Jane')
        self.assertEqual(self.account.programs.count(), 2)
        self.assertEqual(self.account.classes.count(), 3)

    def test_no_semesters_is_invalid(self):
        response = self.setup(self.body(semesters=[]))

        self.assertEqual(response.status_code, 400)
        self.assertIn('no_semester', [i['code'] for i in response.json()['issues']])
        self.assertFalse(self.account.programs.exists())

    def test_rejects_malformed_input(self):
        response = self.setup(self.body(
            programs=['nope'],
            semesters=[{'term': 'autumn', 'year': 'x', 'courses': ['ZZ 999']}],
        ))

        self.assertEqual(response.status_code, 400)
        errors = response.json()
        self.assertIn('programs', errors)
        self.assertEqual(set(errors['semesters'][0]), {'term', 'year', 'courses'})

    def test_rejects_duplicates(self):
        dup_programs = self.body(programs=['computer-science-bs', 'computer-science-bs'])
        dup_semesters = self.body(semesters=[
            {'term': 'fall', 'year': 2025, 'courses': []},
            {'term': 'fall', 'year': 2025, 'courses': []},
        ])

        self.assertEqual(self.setup(dup_programs).status_code, 400)
        self.assertEqual(self.setup(dup_semesters).status_code, 400)
        self.assertFalse(self.account.semesters.exists())

    def test_requires_authentication(self):
        self.client.force_authenticate(None)

        self.assertEqual(self.setup(self.body()).status_code, 401)

    def test_setup_saves_credits_and_pass_fail(self):
        response = self.setup(self.body(semesters=[{'term': 'fall', 'year': 2025, 'courses': [
            {'course': 'CS 149', 'credits_earned': False},
            {'course': 'CS 499', 'credits': 4},
            'MATH 235',
        ]}]))

        self.assertEqual(response.status_code, 200, response.content)
        classes = {c.course.code: c for c in self.account.classes.select_related('course')}
        self.assertEqual((classes['CS 149'].credits, classes['CS 149'].credits_earned), (3, False))
        self.assertEqual((classes['CS 499'].credits, classes['CS 499'].credits_earned), (4, True))
        self.assertEqual((classes['MATH 235'].credits, classes['MATH 235'].credits_earned), (3, True))

        semester = self.client.get('/student/courses/').json()['semesters'][0]
        self.assertEqual((semester['credits_attempted'], semester['credits_earned']), (10, 7))

    def test_variable_credits_default_to_minimum(self):
        self.setup(self.body(semesters=[{'term': 'fall', 'year': 2025, 'courses': ['CS 499']}]))

        self.assertEqual(self.account.classes.get().credits, 1)

    def test_rejects_credits_outside_range(self):
        for course, credits in [('CS 499', 7), ('CS 499', 0), ('CS 149', 4)]:
            response = self.setup(self.body(semesters=[
                {'term': 'fall', 'year': 2025, 'courses': [{'course': course, 'credits': credits}]},
            ]))

            self.assertEqual(response.status_code, 400, (course, credits))
            self.assertIn('credits', response.json()['semesters'][0]['courses']['0'])
        self.assertFalse(self.account.classes.exists())

    def test_failed_classes_dont_count_toward_requirements(self):
        self.setup(self.body(semesters=[{'term': 'fall', 'year': 2025, 'courses': [
            {'course': 'CS 149', 'credits_earned': False},
            {'course': 'CS 499', 'credits': 5},
        ]}]))

        courses = self.client.get('/student/requirements/').json()['courses']
        self.assertEqual([(c['code'], c['credits']) for c in courses], [('CS 499', 5)])
