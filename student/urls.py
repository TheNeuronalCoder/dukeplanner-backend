from django.urls import path

from . import views

urlpatterns = [
    path('courses/', views.courses, name='courses'),
    path('programs/', views.programs, name='programs'),
    path('semesters/', views.semesters, name='semesters'),
    path('requirements/', views.requirements, name='requirements'),
]
