"""court: починить адреса судов Московской области (по хосту определяется суд дела)

Revision ID: b3f7c21a9e04
Revises: f4a92c7b13de
Create Date: 2026-08-07 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b3f7c21a9e04'
down_revision: Union[str, Sequence[str], None] = 'f4a92c7b13de'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Суды Московской области, у которых адрес на исходной странице sudrf.ru побит: либо
# потерялась цифра поддомена (у 50MS0122 стоял 22.mo — сайт ЧУЖОГО, Воскресенского суда),
# либо схема склеилась с хостом. Из-за этого три суда МО делили поддомен с соседями.
#
# Раньше это ничего не ломало, потому что суд выводился из УИД. Теперь суд дела, пришедшего
# ссылкой, определяется по хосту (app/repositories/courts.py: get_by_host), и общий
# поддомен означал бы привязку дела к чужому суду.
#
# Правило восстановления — поддомен равен номеру участка из названия (выполняется у 369
# записей МО из 374), те же пять правок внесены в data/courts.json.
MO_URL_FIXES = {
    "50MS0122": "https://122.mo.msudrf.ru",  # Люберецкий, было 22.mo (Воскресенский)
    "50MS0152": "https://152.mo.msudrf.ru",  # Одинцовский, было 52.mo (Балашихинский)
    "50MS0155": "https://155.mo.msudrf.ru",  # было "http://http:/155.mo.msudrf.ru"
    "50MS0253": "https://253.mo.msudrf.ru",  # было "http://htt253.mo.msudrf.ru"
    "50MS0321": "https://138.mo.msudrf.ru",  # участок № 138, было 38.mo (Домодедовский)
}


def upgrade() -> None:
    """Upgrade schema."""
    connection = op.get_bind()

    for code, base_url in MO_URL_FIXES.items():
        connection.execute(
            sa.text("UPDATE court SET base_url = :base_url WHERE code = :code"),
            {"base_url": base_url, "code": code},
        )

    # Проверяем то, ради чего всё затевалось: у судов msudrf.ru поддомен свой на каждый
    # участок, и делить его двоим нельзя — иначе суд по ссылке не определить.
    collisions = connection.execute(
        sa.text(
            "SELECT lower(substring(base_url from '://([^/]+)')) AS host, "
            "       string_agg(code, ', ' ORDER BY code) AS codes "
            "FROM court WHERE base_url LIKE '%%msudrf.ru%%' "
            "GROUP BY 1 HAVING count(*) > 1"
        )
    ).fetchall()
    if collisions:
        details = "; ".join(f"{row[0]}: {row[1]}" for row in collisions)
        raise RuntimeError(
            f"Один поддомен msudrf.ru на несколько судов ({details}). По хосту определяется "
            f"суд дела, пришедшего ссылкой, поэтому так оставлять нельзя — исправьте адреса "
            f"этих судов и повторите."
        )


def downgrade() -> None:
    """Downgrade schema."""
    # Ничего не возвращаем: побитые адреса были ошибкой данных, а не состоянием схемы,
    # и восстанавливать её незачем.
    pass
