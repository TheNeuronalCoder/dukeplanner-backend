from django.urls import path

from . import views

urlpatterns = [
    path('search/course', views.search_courses, name='search-courses'),
    path('search/program', views.search_programs, name='search-programs'),
]
