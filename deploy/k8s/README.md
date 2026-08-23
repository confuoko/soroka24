# Kubernetes

Манифестов пока нет. Ориентировочный состав, когда дойдёт до них:

- Deployment'ы: `core-v2-api`, `core-v2-worker-urgent`, `core-v2-worker-regular`.
- Инфраструктура: PostgreSQL, RabbitMQ, Redis, S3 (managed или StatefulSet).
- Ingress, ConfigMaps/Secrets, liveness/readiness probes, resource limits.
- KEDA — автоскейл `core-v2-worker-regular` по длине очереди `regular`.

Контейнера beat в списке нет намеренно: расписания обходов в core нет, и решать, какие
дела переобходить, будет сервис, который знает пользователей.
