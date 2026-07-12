"""Точка входа FastAPI-приложения core."""
from fastapi import FastAPI

from app.api.routes import router as cases_router

app = FastAPI(title="Soroka Core", version="0.0.1")

# Подключаем REST-роуты дел (POST /cases, GET /cases/tasks/{id}).
app.include_router(cases_router)


@app.get("/ping")
def ping() -> dict:
    """Health-check: проверка, что сервис жив."""
    return {"message": "pong"}
