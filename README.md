# Soroka — мониторинг мировых судов

Pet-проект: пользователь добавляет ссылку на судебное дело, система парсит его,
раз в день переобходит страницу суда и уведомляет о новых событиях.

Архитектура и план развития — в `~/.claude/plans/linked-watching-horizon.md`.

## Сервисы

| Сервис   | Стек              | Роль                                            |
|----------|-------------------|-------------------------------------------------|
| `core`   | FastAPI + Celery  | парсинг дел, ежедневный мониторинг, детект событий |
| `client` | Django            | регистрация, фронт, боты, уведомления            |

> Сейчас это **каркас**: бизнес-логики, моделей и классов ещё нет — только рабочий
> скелет, который поднимается и отвечает.

## Локальный запуск

```bash
cp .env.example .env
docker-compose up --build
```

Поднимется 7 контейнеров: `postgres`, `rabbitmq`, `redis`, `core-api`,
`core-worker-urgent`, `core-worker-regular`, `client-web`.

## Проверка

- core (FastAPI):     http://localhost:8000/ping  → `{"message": "pong"}`
- client (Django):    http://localhost:8080/       → «Скоро здесь будет проект»
- RabbitMQ UI:        http://localhost:15672        (логин/пароль из `.env`)

В логах `core-worker-urgent` / `core-worker-regular` Celery подключается к RabbitMQ
и слушает очереди `urgent` / `regular` (задач пока нет — это ожидаемо).

## Структура

```
services/core     — FastAPI-приложение + Celery (пустые заготовки под парсеры/мониторинг)
services/client   — Django-проект с одной страницей-заглушкой
deploy/k8s        — манифесты Kubernetes (появятся позже)
```
