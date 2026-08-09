"""Сериализаторы: только перекладывание данных, без логики.

Всё, что делает решения (валидация ссылки, поход в core, переходы состояний),
живёт в apps/monitoring/services.py — общее с вебом и ботом.
"""
from djoser.serializers import UserCreateSerializer as DjoserUserCreateSerializer
from rest_framework import serializers

from apps.accounts.services import register_user
from apps.monitoring.models import MonitoredCase


class UserCreateSerializer(DjoserUserCreateSerializer):
    """Регистрация через API.

    Штатный сериализатор djoser создаёт только пользователя, а у нас пользователь
    без подписки — состояние, которого не бывает. Подменяем единственный шаг, где
    он появляется, на общий register_user: тогда веб и API заводят аккаунт
    одинаково.
    """

    def perform_create(self, validated_data):
        validated_data.pop("re_password", None)
        return register_user(
            username=validated_data["username"],
            password=validated_data["password"],
            email=validated_data.get("email"),
        )


class MonitoredCaseSerializer(serializers.ModelSerializer):
    state_display = serializers.CharField(source="get_state_display", read_only=True)

    class Meta:
        model = MonitoredCase
        fields = (
            "id",
            "source_url",
            "state",
            "state_display",
            "core_case_id",
            "core_status",
            "core_changed_at",
            "core_last_checked_at",
            "last_error",
            "created_at",
        )
        read_only_fields = fields


class AddCaseSerializer(serializers.Serializer):
    """Вход POST /api/v1/cases/ — одна ссылка на карточку дела."""

    url = serializers.CharField(max_length=1000)
