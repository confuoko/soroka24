"""Точка входа FastAPI-приложения core_v2.

ВАЖНО про async. Все роуты объявлены обычным `def`, а не `async def`, и так должно
остаться. Походы на порталы судов идут через Playwright sync API, а `sync_playwright()`
падает, если его вызвать внутри работающего event loop. FastAPI выполняет обычный `def`
в отдельном потоке — там event loop'а нет, и всё работает. Первый же `async def` на пути
до похода в суд сломает это молча и не сразу.
"""
from fastapi import FastAPI

from app.admin import setup_admin
from app.api.cases import router as cases_router
from app.api.events import router as events_router
from app.api.search_case import router as search_case_router

app = FastAPI(title="Soroka Core v2", version="0.0.1")

# Запуск обхода и слежение за задачей: POST /search_case, GET /search_case/tasks/{id}.
app.include_router(search_case_router)
# Чтение карточки дела: GET /cases/{id}, GET /cases/{id}/summary.
app.include_router(cases_router)
# Чтение потока изменений: GET /cases/{id}/events.
app.include_router(events_router)

# Админка SQLAdmin на /admin: посмотреть и поправить записи глазами.
setup_admin(app)


@app.get("/ping")
def ping() -> dict:
    """Health-check: проверка, что сервис жив."""
    return {"message": "pong"}
