"""Точка входа FastAPI-приложения core."""
from fastapi import FastAPI

from app.admin import setup_admin
from app.api.routes import router as cases_router

app = FastAPI(title="Soroka Core", version="0.0.1")

# Подключаем REST-роуты дел (POST /search_case, GET /search_case/tasks/{id}).
app.include_router(cases_router)

# Подключаем админку SQLAdmin на /admin (просмотр и правка записей моделей).
setup_admin(app)


@app.get("/ping")
def ping() -> dict:
    """Health-check: проверка, что сервис жив."""
    return {"message": "pong"}
