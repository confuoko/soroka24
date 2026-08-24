"""Адреса клиентского сервиса.

В адресе дела стоит `core_case_id`, а не первичный ключ подписки: ссылка на дело должна
выглядеть как ссылка на дело, а не на нашу служебную строку. Заодно такой адрес не
меняется, если подписку пересоздать.
"""
from django.urls import path

from cases import views

urlpatterns = [
    path("", views.MyCasesView.as_view(), name="my-cases"),
    path("cases/add/", views.AddCaseView.as_view(), name="add-case"),
    path("cases/pending/<int:pk>/", views.PendingSearchView.as_view(), name="pending-search"),
    path("cases/<int:core_case_id>/", views.CaseDetailView.as_view(), name="case-detail"),
    path(
        "cases/<int:core_case_id>/unsubscribe/",
        views.UnsubscribeView.as_view(),
        name="unsubscribe",
    ),
]
