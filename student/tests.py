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
        Course.objects.create(prefix_id='CS', number='149', title='Intro', credits=3,
                              min_credits=3, max_credits=3)

    def setUp(self):
        self.account = Account.objects.create_user('student', 'S', 'T')
        self.client = APIClient()
        self.client.force_authenticate(self.account)

    def post(self, path, body):
        return self.client.post(f'/student/{path}/', body, format='json')

    def delete(self, path, body):
        return self.client.delete(f'/student/{path}/', body, format='json')

    def set_up_valid(self):
        Curriculum.objects.create(account=self.account, program_id='computer-science-bs')
        Curriculum.objects.create(account=self.account, program_id='philosophy-minor')
        return Semester.objects.create(account=self.account, term=Semester.Term.FALL, year=2025)

    def codes(self, response):
        return [i['code'] for i in response.json()['issues']]

    def test_setup_from_empty_account_is_allowed(self):
        # Errors the account already had (no degree, no semester) don't block.
        self.assertEqual(self.post('programs/add', [{'program': 'philosophy-minor'}]).status_code, 201)
        self.assertEqual(self.post('semesters/add', [{'term': 'fall', 'year': 2025}]).status_code, 201)
        self.assertEqual(self.post('programs/add', [{'program': 'computer-science-bs'}]).status_code, 201)

    def test_cannot_delete_only_degree(self):
        self.set_up_valid()
        r = self.delete('programs/delete', ['computer-science-bs'])
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self.codes(r), ['no_degree'])
        self.assertEqual(self.account.programs.count(), 2)

    def test_can_delete_minor(self):
        self.set_up_valid()
        self.assertEqual(self.delete('programs/delete', ['philosophy-minor']).status_code, 200)

    def test_cannot_add_minor_in_same_area_as_major(self):
        self.set_up_valid()
        r = self.post('programs/add', [{'program': 'computer-science-minor'}])
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self.codes(r), ['major_minor_same_area'])
        self.assertFalse(self.account.programs.filter(program_id='computer-science-minor').exists())

    def test_cannot_delete_only_semester(self):
        semester = self.set_up_valid()
        r = self.delete('semesters/delete', [semester.pk])
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self.codes(r), ['no_semester'])
        self.assertTrue(Semester.objects.filter(pk=semester.pk).exists())

    def test_can_delete_one_of_two_semesters(self):
        semester = self.set_up_valid()
        Semester.objects.create(account=self.account, term=Semester.Term.SPRING, year=2026)
        self.assertEqual(self.delete('semesters/delete', [semester.pk]).status_code, 200)

    def test_cannot_add_semester_out_of_range(self):
        self.set_up_valid()
        r = self.post('semesters/add', [{'term': 'spring', 'year': 3000}])
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self.account.semesters.count(), 1)

    def test_classes(self):
        semester = self.set_up_valid()
        r = self.post('courses/add', [{'course': 'CS 149', 'semester': semester.pk}])
        self.assertEqual(r.status_code, 201)

        # An unassigned class is a validation error.
        r = self.post('courses/add', [{'course': 'CS 149'}])
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self.codes(r), ['class_without_semester'])
        self.assertEqual(Class.objects.filter(account=self.account).count(), 1)

        self.assertEqual(self.delete('courses/delete', [Class.objects.get().pk]).status_code, 200)

    def test_deleting_last_class_deletes_semester(self):
        semester = self.set_up_valid()
        other = Semester.objects.create(account=self.account, term=Semester.Term.SPRING, year=2026)
        c = Class.objects.create(account=self.account, course=Course.objects.get(), semester=semester, credits=3)
        Class.objects.create(account=self.account, course=Course.objects.get(), semester=other, credits=3)
        self.assertEqual(self.delete('courses/delete', [c.pk]).status_code, 200)
        self.assertFalse(Semester.objects.filter(pk=semester.pk).exists())
        self.assertTrue(Semester.objects.filter(pk=other.pk).exists())

    def test_deleting_last_class_keeps_only_semester(self):
        semester = self.set_up_valid()
        c = Class.objects.create(account=self.account, course=Course.objects.get(), semester=semester, credits=3)
        self.assertEqual(self.delete('courses/delete', [c.pk]).status_code, 200)
        self.assertTrue(Semester.objects.filter(pk=semester.pk).exists())
