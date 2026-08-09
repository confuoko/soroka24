"""Корневая страница.

Отдельного лендинга пока нет, поэтому корень просто ведёт туда, где идёт работа:
вошедшего — к его делам, гостя — на вход (за редиректом следит сам case_list).
Страница-заглушка coming_soon осталась для случая, когда лендинг понадобится.
"""
from django.shortcuts import redirect, render


def home(request):
    return redirect("case_list")


def coming_soon(request):
    return render(request, "coming_soon.html")
