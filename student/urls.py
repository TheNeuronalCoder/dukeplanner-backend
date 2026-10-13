from django.urls import path

from . import views

urlpatterns = [
    path('courses/', views.courses, name='courses'),
    path('courses/add/', views.add_courses, name='add-courses'),
    path('courses/delete/', views.delete_courses, name='delete-courses'),
    path('programs/add/', views.add_programs, name='add-programs'),
    path('programs/delete/', views.delete_programs, name='delete-programs'),
    path('semesters/add/', views.add_semesters, name='add-semesters'),
    path('semesters/delete/', views.delete_semesters, name='delete-semesters'),
    path('requirements/', views.requirements, name='requirements'),
]
