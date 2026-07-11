"""View'ы client. Пока только страница-заглушка (без моделей и логики)."""
from django.shortcuts import render


def coming_soon(request):
    return render(request, "coming_soon.html")
