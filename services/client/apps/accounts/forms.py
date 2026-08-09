"""Формы регистрации.

Берём готовую UserCreationForm из django.contrib.auth: в ней уже есть повтор
пароля, проверка уникальности логина и прогон через AUTH_PASSWORD_VALIDATORS.
Своего кода аутентификации не пишем.
"""
from django.contrib.auth.forms import UserCreationForm

from apps.accounts.models import User


class SignupForm(UserCreationForm):
    class Meta(UserCreationForm.Meta):
        model = User
        fields = ("username", "email")
