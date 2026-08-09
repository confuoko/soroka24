from django.apps import AppConfig


class AccountsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.accounts"
    # Метка короткая: на неё ссылается AUTH_USER_MODEL = "accounts.User".
    label = "accounts"
    verbose_name = "Пользователи"
