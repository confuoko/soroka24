"""Точка входа FastAPI-приложения core.

Пока здесь только health-check /ping. REST-роуты дел появятся в app/api/.
"""
from fastapi import FastAPI

app = FastAPI(title="Soroka Core", version="0.0.1")


@app.get("/ping")
def ping() -> dict:
    return {"message": "pong"}
