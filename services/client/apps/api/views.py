"""API дел на мониторинге.

Ровно те же вызовы, что и во views.py веба, — разница только в формате ответа.
Телеграм-бот пойдёт сюда по токену (или напрямую в services.py, если будет жить
в этом же процессе).
"""
from django.core.exceptions import ValidationError as DjangoValidationError
from django.shortcuts import get_object_or_404
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.response import Response

from apps.api.serializers import AddCaseSerializer, MonitoredCaseSerializer
from apps.monitoring.models import MonitoredCase
from apps.monitoring.services import (
    add_case_to_monitoring,
    list_monitored_cases,
    refresh_from_core,
    remove_from_monitoring,
)


class MonitoredCaseViewSet(viewsets.ViewSet):
    """Дела текущего пользователя.

    ViewSet, а не ModelViewSet: создание и удаление идут не через ORM напрямую, а
    через сервисный слой (там поход в core и переключение мониторинга).
    """

    def list(self, request):
        cases = list_monitored_cases(request.user)
        return Response(MonitoredCaseSerializer(cases, many=True).data)

    def retrieve(self, request, pk=None):
        monitored = self._get(request, pk)
        return Response(MonitoredCaseSerializer(monitored).data)

    def create(self, request):
        payload = AddCaseSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        try:
            monitored = add_case_to_monitoring(request.user, payload.validated_data["url"])
        except DjangoValidationError as exc:
            # Отказ core приходит из сервисного слоя доменным исключением Django —
            # переводим его в 400 с тем же текстом, что видит пользователь в вебе.
            raise DRFValidationError({"url": list(exc.messages)}) from exc
        return Response(
            MonitoredCaseSerializer(monitored).data, status=status.HTTP_201_CREATED
        )

    def destroy(self, request, pk=None):
        self._get(request, pk)
        remove_from_monitoring(request.user, int(pk))
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=["post"])
    def refresh(self, request, pk=None):
        """Обновить дело из core, не дожидаясь фоновой задачи."""
        monitored = refresh_from_core(self._get(request, pk))
        return Response(MonitoredCaseSerializer(monitored).data)

    def _get(self, request, pk) -> MonitoredCase:
        """Дело текущего пользователя или 404. Фильтр по user — проверка прав."""
        return get_object_or_404(MonitoredCase, pk=pk, user=request.user)
