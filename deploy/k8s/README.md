# Kubernetes

Манифесты появятся на этапе 7 плана. Ориентировочный состав:

- Deployment'ы: `core-api`, `client-web`, `core-worker-urgent`, `core-worker-regular`,
  `celery-beat`, `telegram-bot`.
- Инфраструктура: PostgreSQL, RabbitMQ, Redis (managed или StatefulSet).
- Ingress, ConfigMaps/Secrets, liveness/readiness probes, resource limits.
- KEDA — автоскейл `core-worker-regular` по длине очереди `regular`.
