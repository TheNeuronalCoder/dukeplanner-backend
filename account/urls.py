from django.urls import path

from . import views

urlpatterns = [
    path('request-code/', views.request_code, name='request-code'),
    path('verify-code/', views.verify_code, name='verify-code'),
]
