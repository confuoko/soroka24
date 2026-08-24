# Образ клиентского сервиса Soroka24.
#
# Тонкий сознательно: ни Playwright, ни браузера здесь нет и быть не должно. На порталы
# судов ходит core_v2, а этот сервис знает только пользователей, подписки и HTTP к core.
FROM python:3.12-slim

# PYTHONUNBUFFERED: без него логи Django лежат в буфере и в `docker compose logs` не видны,
# пока буфер не заполнится — процесс выглядит мёртвым.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Зависимости отдельным слоем: правка кода не пересобирает pip install.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

# Команду задаёт docker-compose: у web и consumer один образ и разные процессы.
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000"]
