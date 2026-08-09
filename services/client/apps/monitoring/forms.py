"""Форма добавления дела.

Валидацию ссылки и всё остальное делает сервисный слой — форма нужна только
чтобы принять поле и показать ошибку. Дублировать проверки здесь нельзя: тогда у
веба и бота они разъедутся.
"""
from django import forms


class AddCaseForm(forms.Form):
    url = forms.CharField(
        label="Ссылка на дело",
        max_length=1000,
        widget=forms.URLInput(
            attrs={
                "placeholder": "https://95.mo.msudrf.ru/modules.php?name=sud_delo&op=cs&...",
                "size": 80,
            }
        ),
    )
