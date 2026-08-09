"""Веб-страницы регистрации.

Вход и выход — штатные LoginView/LogoutView, они подключены в config/urls.py.
Здесь только регистрация, потому что вместе с пользователем надо завести подписку;
делает это register_user, а view остаётся тонкой обёрткой над ним.
"""
from django.contrib.auth import login
from django.shortcuts import redirect, render
from django.urls import reverse

from apps.accounts.forms import SignupForm
from apps.accounts.services import register_user


def signup(request):
    if request.user.is_authenticated:
        return redirect("case_list")

    if request.method != "POST":
        return render(request, "registration/signup.html", {"form": SignupForm()})

    form = SignupForm(request.POST)
    if not form.is_valid():
        return render(request, "registration/signup.html", {"form": form})

    # Форма уже проверила пароль и уникальность логина, но пользователя создаёт не
    # она, а сервисный слой: только там вместе с пользователем заводится подписка.
    user = register_user(
        username=form.cleaned_data["username"],
        password=form.cleaned_data["password1"],
        email=form.cleaned_data.get("email"),
    )
    login(request, user)
    return redirect(reverse("case_list"))
