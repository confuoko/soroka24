"""Views клиентского сервиса — стандартные Django CBV.

Своей иерархии классов здесь нет и не будет (ТЗ §13): `ListView`, `FormView`, `DetailView`
берутся готовыми, а всё, что не влезает в них, — обычные функции в cases/monitoring.py.

Общая черта всех страниц: судебные данные приходят из core по HTTP в момент показа. У
Django своей копии нет, поэтому «дело» здесь — это всегда ПОДПИСКА, к которой в контексте
приложена витрина или карточка из core. Отсюда и `DetailView` над `CaseSubscription`, а не
над делом: заодно это бесплатная проверка прав — чужую подписку queryset не отдаст.

Отказ core нигде не превращается в 500. Недоступность соседнего сервиса — ожидаемое
состояние: страница показывает то, что знает сама, и честно говорит, что остального сейчас
не видно.
"""
import logging

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import get_object_or_404, redirect
from django.views.generic import DetailView, FormView, ListView, View

from cases import monitoring
from cases.forms import AddCaseForm
from cases.integrations import core
from cases.models import CaseSubscription, PendingCaseSearch

logger = logging.getLogger(__name__)


class MyCasesView(LoginRequiredMixin, ListView):
    """«Мои дела»: подписки пользователя с витринами из core.

    Витрины забираются ОДНИМ запросом на всю страницу (`GET /cases?ids=`). Иначе страница
    с тридцатью подписками стоила бы тридцать последовательных обращений к core — N+1,
    только по сети, где каждый шаг стоит десятки миллисекунд, а не микросекунды.
    """

    model = CaseSubscription
    context_object_name = "subscriptions"
    template_name = "cases/my_cases.html"

    def get_queryset(self):
        return CaseSubscription.objects.filter(
            user=self.request.user, is_active=True
        ).order_by("-created_at")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        subscriptions = context["subscriptions"]

        try:
            summaries = core.summaries_by_id(
                subscription.core_case_id for subscription in subscriptions
            )
        except core.CoreUnavailable:
            # Список подписок — наш, и показать его мы обязаны даже когда core молчит.
            # Строки останутся без номера и статуса, зато человек увидит, что дела на
            # месте, а не пустую страницу.
            summaries = {}
            context["core_unavailable"] = True

        # Раскладываем витрины по подпискам здесь, а не в шаблоне: шаблону нечем
        # обработать отсутствующую витрину, а нам есть чем — показать «дела нет в core».
        context["rows"] = [
            {
                "subscription": subscription,
                "summary": summaries.get(subscription.core_case_id),
            }
            for subscription in subscriptions
        ]
        # Дела, которые ещё ищутся: у них нет ни номера, ни УИД — только то, что ввёл
        # пользователь. Без этой строки они бы просто не существовали на экране, и человек
        # решил бы, что добавление не сработало.
        context["pending"] = PendingCaseSearch.objects.filter(
            user=self.request.user, resolved_at__isnull=True
        ).order_by("-created_at")
        return context


class AddCaseView(LoginRequiredMixin, FormView):
    """«Добавить дело»: ссылка или УИД → подписка либо ожидание.

    Ветвление по пяти исходам `POST /search_case`. Текст отказа берём из ответа core как
    есть: он написан человеческим языком и знает про конкретный суд («Судебный участок
    № 235: поиск по УИД на этом портале не поддерживается»). Сочинять свой — значит терять
    эти подробности.
    """

    form_class = AddCaseForm
    template_name = "cases/add_case.html"

    def form_valid(self, form):
        query = form.cleaned_data["query"]

        try:
            answer = core.search_case(query)
        except core.CoreUnavailable:
            form.add_error(
                None,
                "Сервис поиска дел сейчас недоступен. Попробуйте через несколько минут.",
            )
            return self.form_invalid(form)

        status = answer.get("status")
        message = answer.get("message")
        case_ids = answer.get("case_ids") or []
        if answer.get("case_id") and answer["case_id"] not in case_ids:
            case_ids = [answer["case_id"], *case_ids]

        # Два действия, и они НЕ исключают друг друга. По одному УИД карточек бывает
        # несколько: часть могла найтись сразу, а поиск на портале всё равно запущен —
        # ветка по УИД так и отвечает (status=processing вместе с непустым case_ids).
        # Поэтому сначала делаем оба, и только потом решаем, куда вести.
        if case_ids:
            monitoring.subscribe(self.request.user, case_ids)

        pending = None
        if status == "processing" and answer.get("task_id"):
            pending, _ = PendingCaseSearch.objects.get_or_create(
                user=self.request.user,
                core_task_id=answer["task_id"],
                defaults={"query": query},
            )

        if pending is not None:
            if case_ids:
                messages.success(
                    self.request,
                    f"Нашли карточек: {len(case_ids)}. Ищем остальные по этому УИД.",
                )
            return redirect("pending-search", pk=pending.pk)

        if len(case_ids) == 1:
            messages.success(self.request, "Дело добавлено, следим за изменениями.")
            return redirect("case-detail", core_case_id=case_ids[0])

        if case_ids:
            messages.success(
                self.request,
                f"По этому запросу нашлось несколько карточек ({len(case_ids)}) — "
                "добавили все.",
            )
            return redirect("my-cases")

        # Дел не нашлось и поиск не запущен — значит, отказ. Их четыре: два про формат
        # ввода и два про суд.
        if status in ("invalid_query", "invalid_uid"):
            form.add_error("query", message or "Не похоже ни на ссылку, ни на УИД дела.")
        elif status in ("link_required", "unsupported_court"):
            # Это не ошибка ввода: пользователь всё написал правильно, просто портал так
            # не умеет. Поэтому ошибка формы, а не поля.
            form.add_error(None, message or "С этим судом пока не получится.")
        else:
            logger.warning("core вернул неизвестный status=%r на %r", status, query)
            form.add_error(None, message or "Не удалось добавить дело.")

        return self.form_invalid(form)


class PendingSearchView(LoginRequiredMixin, DetailView):
    """Страница ожидания: «ищем дело».

    Обновляется сама через `<meta http-equiv="refresh">` — без JS. Это не эстетический
    выбор, а признание того, что фронтенд пока не цель (ТЗ §9): страница-заглушка на
    метатеге делает ровно то, что нужно, и её нечему ломаться.

    Каждое обновление спрашивает core о судьбе задачи и, если та завершилась, создаёт
    подписку. Это БЫСТРЫЙ путь. Закрытую вкладку добирает `resolve_pending_searches` —
    поэтому ожидание и хранится в базе, а не только в URL.
    """

    model = PendingCaseSearch
    context_object_name = "pending"
    template_name = "cases/pending_search.html"

    def get_queryset(self):
        # Только свои ожидания: чужое id в URL не должно ничего показывать.
        return PendingCaseSearch.objects.filter(user=self.request.user)

    def get(self, request, *args, **kwargs):
        pending = self.get_object()
        pending = monitoring.resolve_pending(pending)

        if not pending.is_pending and not pending.last_error:
            # Нашли. Уводим на список: какая именно карточка получилась, там видно, а
            # ожидание больше не нужно.
            messages.success(request, "Дело найдено, следим за изменениями.")
            return redirect("my-cases")

        self.object = pending
        return self.render_to_response(self.get_context_data(object=pending))

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        pending = context["pending"]
        # Состояние задачи — чтобы показать число попыток: пока дело ищется, это
        # единственное, что вообще можно показать. Ни номера, ни УИД ещё нет.
        try:
            context["task"] = core.get_search_task(pending.core_task_id)
        except core.CoreUnavailable:
            context["task"] = None
        return context


class CaseDetailView(LoginRequiredMixin, DetailView):
    """Страница дела: судебные данные из core, права — из подписки.

    `DetailView` над ПОДПИСКОЙ, а не над делом: Django-модели `Case` здесь нет и быть не
    должно. Побочная выгода — проверка прав достаётся бесплатно: queryset ограничен своими
    подписками, поэтому чужое дело отдаст 404, а не карточку.
    """

    model = CaseSubscription
    context_object_name = "subscription"
    template_name = "cases/case_detail.html"
    # В URL стоит id дела в core, а не первичный ключ подписки: ссылка на дело должна
    # выглядеть как ссылка на дело.
    slug_field = "core_case_id"
    slug_url_kwarg = "core_case_id"

    def get_queryset(self):
        return CaseSubscription.objects.filter(user=self.request.user)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        case_id = context["subscription"].core_case_id
        try:
            context["case"] = core.get_case(case_id)
        except core.CoreUnavailable:
            # Карточку показать нечем — но сказать об этом надо честно, а не отдавать 500.
            context["case"] = None
            context["core_unavailable"] = True
        # Отметка изменений прочитанными появится здесь в Phase 6 (UserCaseChange).
        return context


class UnsubscribeView(LoginRequiredMixin, View):
    """Перестать следить за делом.

    POST, а не GET: операция меняет состояние, и ссылка, по которой браузер или его
    префетчер могут пройти сами, для этого не годится.
    """

    def post(self, request, core_case_id: int):
        subscription = get_object_or_404(
            CaseSubscription, user=request.user, core_case_id=core_case_id
        )
        monitoring.unsubscribe(subscription)
        messages.success(request, "Больше не следим за этим делом.")
        return redirect("my-cases")
