from django.urls import path
from rest_framework_simplejwt.views import TokenRefreshView, TokenBlacklistView

from . import views


urlpatterns = [
    path('request-code/', views.request_code, name='request-code'),
    path('verify-code/', views.verify_code, name='verify-code'),
    path('refresh/', TokenRefreshView.as_view(), name='token-refresh'),
    path('logout/', TokenBlacklistView.as_view(), name='token-blacklist'),
]
