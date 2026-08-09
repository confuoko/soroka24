"""Веб-страницы списка дел.

Тонкие обёртки: вся логика — в services.py, чтобы её без изменений вызвал бот.
"""
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import Http404
from django.shortcuts import redirect, render

from apps.monitoring.forms import AddCaseForm
from apps.monitoring.models import MonitoredCase
from apps.monitoring.services import (
    add_case_to_monitoring,
    get_monitored_case,
    list_monitored_cases,
    refresh_from_core,
    remove_from_monitoring,
)


@login_required
def case_list(request):
    return render(
        request,
        "monitoring/case_list.html",
        {"cases": list_monitored_cases(request.user), "form": AddCaseForm()},
    )


@login_required
def case_add(request):
    if request.method != "POST":
        return render(request, "monitoring/case_add.html", {"form": AddCaseForm()})

    form = AddCaseForm(request.POST)
    if not form.is_valid():
        return render(request, "monitoring/case_add.html", {"form": form})

    try:
        add_case_to_monitoring(request.user, form.cleaned_data["url"])
    except ValidationError as exc:
        # Отказ core (суд не поддержан, адрес не разобран) показываем как ошибку поля.
        form.add_error("url", exc)
        return render(request, "monitoring/case_add.html", {"form": form})

    return redirect("case_list")


@login_required
def case_detail(request, pk: int):
    try:
        monitored = get_monitored_case(request.user, pk)
    except MonitoredCase.DoesNotExist:
        raise Http404("Дело не найдено")
    return render(request, "monitoring/case_detail.html", {"case": monitored})


@login_required
def case_refresh(request, pk: int):
    """Обновить дело из core по кнопке (не дожидаясь фоновой задачи)."""
    if request.method != "POST":
        return redirect("case_detail", pk=pk)
    try:
        monitored = get_monitored_case(request.user, pk)
    except MonitoredCase.DoesNotExist:
        raise Http404("Дело не найдено")
    refresh_from_core(monitored)
    return redirect("case_detail", pk=pk)


@login_required
def case_delete(request, pk: int):
    if request.method != "POST":
        return redirect("case_detail", pk=pk)
    try:
        remove_from_monitoring(request.user, pk)
    except MonitoredCase.DoesNotExist:
        raise Http404("Дело не найдено")
    return redirect("case_list")
