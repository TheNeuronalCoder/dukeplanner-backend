from django.urls import path
from rest_framework_simplejwt.views import TokenRefreshView, TokenBlacklistView

from . import views


urlpatterns = [
    path('auth/request-code/', views.request_code, name='request-code'),
    path('auth/verify-code/', views.verify_code, name='verify-code'),
    path('auth/refresh/', TokenRefreshView.as_view(), name='token-refresh'),
    path('auth/logout/', TokenBlacklistView.as_view(), name='token-blacklist'),
    path('account/setup/', views.setup, name='setup'),
]
