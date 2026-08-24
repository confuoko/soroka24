-- Отдельная база для core_v2.
--
-- База из POSTGRES_DB создаётся самим образом postgres; эту надо завести руками.
--
-- Своя, а не общая со старым core: alembic_version — однострочная таблица, поэтому две
-- независимые истории миграций в одной базе несовместимы. Побочная польза — старый core
-- можно поднять рядом и сверить поведение, не мешая данным core_v2.
--
-- Скрипт выполняется ТОЛЬКО при первой инициализации кластера (пустой том
-- postgres_data). Если база уже поднималась, заведите вручную:
--   docker compose exec postgres psql -U soroka -c "CREATE DATABASE soroka_core_v2;"
CREATE DATABASE soroka_core_v2;

-- Отдельная база для клиентского сервиса (Django).
--
-- Тоже своя: в одной базе не могут жить две независимые истории миграций — alembic у core
-- и django_migrations у клиента. Судебных данных здесь нет вовсе, только пользователи,
-- подписки и unread.
--
-- Если база уже поднималась:
--   docker compose exec postgres psql -U soroka -c "CREATE DATABASE soroka_client;"
CREATE DATABASE soroka_client;
