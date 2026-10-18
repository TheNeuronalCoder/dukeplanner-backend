from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from account.models import Account
from catalog.models import Course, Program, Subject

from .models import Class, Curriculum, Semester


@override_settings(ALLOWED_HOSTS=['*'])
class GuardTests(TestCase):
    """Adds and deletes that would make the account invalid are rejected."""

    @classmethod
    def setUpTestData(cls):
        for slug, type_ in [
            ('computer-science-bs', 'B.S.'),
            ('computer-science-minor', 'Minor'),
            ('philosophy-minor', 'Minor'),
        ]:
            Program.objects.create(
                slug=slug, name=slug, level='undergraduate', type=type_,
                url='https://example.com', requirements_pdf_url='https://example.com',
            )
        Subject.objects.create(code='CS', name='Computer Science')
        for number in ['149', '159', '227']:
            Course.objects.create(prefix_id='CS', number=number, title=number, credits=3,
                                  min_credits=3, max_credits=4 if number == '227' else 3)

    def setUp(self):
        self.account = Account.objects.create_user('student', 'S', 'T')
        self.client = APIClient()
        self.client.force_authenticate(self.account)

    def post(self, path, body):
        return self.client.post(f'/student/{path}/', body, format='json')

    def patch(self, path, body):
        return self.client.patch(f'/student/{path}/', body, format='json')

    def delete(self, path, body):
        return self.client.delete(f'/student/{path}/', body, format='json')

    def set_up_valid(self):
        Curriculum.objects.create(account=self.account, program_id='computer-science-bs')
        Curriculum.objects.create(account=self.account, program_id='philosophy-minor')
        semester = Semester.objects.create(account=self.account, term=Semester.Term.FALL, year=2025)
        self.add_class(semester)
        return semester

    def add_class(self, semester, course='CS 149'):
        prefix, number = course.split()
        return Class.objects.create(
            account=self.account, course=Course.objects.get(prefix_id=prefix, number=number),
            semester=semester, credits=3,
        )

    def codes(self, response):
        return [i['code'] for i in response.json()['issues']]

    def test_setup_from_empty_account_is_allowed(self):
        # Errors the account already had (no degree, no semester) don't block.
        self.assertEqual(self.post('programs', [{'program': 'philosophy-minor'}]).status_code, 201)
        r = self.post('semesters', [{'term': 'fall', 'year': 2025, 'courses': ['CS 149']}])
        self.assertEqual(r.status_code, 201)
        self.assertEqual(self.post('programs', [{'program': 'computer-science-bs'}]).status_code, 201)
        self.assertEqual(Semester.objects.get().classes.get().course.code, 'CS 149')

    def test_cannot_add_empty_semester(self):
        self.set_up_valid()
        for body in [{'term': 'spring', 'year': 2026}, {'term': 'spring', 'year': 2026, 'courses': []}]:
            r = self.post('semesters', [body])
            self.assertEqual(r.status_code, 400)
            self.assertIn('courses', r.json()[0])
        self.assertEqual(self.account.semesters.count(), 1)

    def test_cannot_add_course_twice_in_new_semester(self):
        self.set_up_valid()
        r = self.post('semesters', [{'term': 'spring', 'year': 2026, 'courses': ['CS 149', 'CS 149']}])
        self.assertEqual(r.status_code, 400)

    def test_cannot_delete_only_degree(self):
        self.set_up_valid()
        r = self.delete('programs', ['computer-science-bs'])
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self.codes(r), ['no_degree'])
        self.assertEqual(self.account.programs.count(), 2)

    def test_can_delete_minor(self):
        self.set_up_valid()
        self.assertEqual(self.delete('programs', ['philosophy-minor']).status_code, 200)

    def test_cannot_add_minor_in_same_area_as_major(self):
        self.set_up_valid()
        r = self.post('programs', [{'program': 'computer-science-minor'}])
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self.codes(r), ['major_minor_same_area'])
        self.assertFalse(self.account.programs.filter(program_id='computer-science-minor').exists())

    def test_cannot_delete_only_semester(self):
        semester = self.set_up_valid()
        r = self.delete('semesters', [semester.pk])
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self.codes(r), ['no_semester'])
        self.assertTrue(Semester.objects.filter(pk=semester.pk).exists())

    def test_can_delete_one_of_two_semesters(self):
        semester = self.set_up_valid()
        self.add_class(Semester.objects.create(account=self.account, term=Semester.Term.SPRING, year=2026))
        self.assertEqual(self.delete('semesters', [semester.pk]).status_code, 200)

    def test_cannot_add_semester_out_of_range(self):
        self.set_up_valid()
        r = self.post('semesters', [{'term': 'spring', 'year': 3000, 'courses': ['CS 149']}])
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self.account.semesters.count(), 1)

    def test_classes(self):
        semester = self.set_up_valid()
        r = self.post('courses', [{'course': 'CS 227', 'semester': semester.pk}])
        self.assertEqual(r.status_code, 201)

        # A class must be placed in a semester.
        self.assertEqual(self.post('courses', [{'course': 'CS 227'}]).status_code, 400)
        # And can't be listed twice in one.
        r = self.post('courses', [{'course': 'CS 149', 'semester': semester.pk}])
        self.assertEqual(r.status_code, 400)
        self.assertEqual(Class.objects.filter(account=self.account).count(), 2)

        self.assertEqual(self.delete('courses', [Class.objects.get(course__number='227').pk]).status_code, 200)

    def test_update_class(self):
        semester = self.set_up_valid()
        c = self.add_class(semester, 'CS 227')
        r = self.patch('courses', [{'id': c.pk, 'credits': 4, 'credits_earned': False}])
        self.assertEqual(r.status_code, 200)
        c.refresh_from_db()
        self.assertEqual((c.credits, c.credits_earned), (4, False))

        # Changing the course resets the credits to its own.
        self.assertEqual(self.patch('courses', [{'id': c.pk, 'course': 'CS 159'}]).status_code, 200)
        c.refresh_from_db()
        self.assertEqual((c.course.code, c.credits, c.credits_earned), ('CS 159', 3, False))

        self.assertEqual(self.patch('courses', [{'id': c.pk, 'credits': 9}]).status_code, 400)
        # Duplicates in a semester are rejected by validation.
        r = self.patch('courses', [{'id': c.pk, 'course': 'CS 149'}])
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self.codes(r), ['duplicate_class_in_semester'])

    def test_moving_last_class_deletes_semester(self):
        semester = self.set_up_valid()
        other = Semester.objects.create(account=self.account, term=Semester.Term.SPRING, year=2026)
        c = self.add_class(other, 'CS 227')
        self.assertEqual(self.patch('courses', [{'id': c.pk, 'semester': semester.pk}]).status_code, 200)
        self.assertFalse(Semester.objects.filter(pk=other.pk).exists())

    def test_deleting_last_class_deletes_semester(self):
        semester = self.set_up_valid()
        other = Semester.objects.create(account=self.account, term=Semester.Term.SPRING, year=2026)
        self.add_class(other)
        self.assertEqual(self.delete('courses', [semester.classes.get().pk]).status_code, 200)
        self.assertFalse(Semester.objects.filter(pk=semester.pk).exists())
        self.assertTrue(Semester.objects.filter(pk=other.pk).exists())

    def test_cannot_delete_last_class_of_only_semester(self):
        semester = self.set_up_valid()
        r = self.delete('courses', [semester.classes.get().pk])
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self.codes(r), ['no_semester'])
        self.assertTrue(semester.classes.exists())
