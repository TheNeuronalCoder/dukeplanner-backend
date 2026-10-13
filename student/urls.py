from django.urls import path

from . import views

urlpatterns = [
    path('requirements/', views.requirements, name='requirements'),
]
