"""proxy: до каких порталов доходит адрес (portals)

Revision ID: b8e1f4a06d27
Revises: a7d4f2c81b39
Create Date: 2026-08-20 14:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'b8e1f4a06d27'
down_revision: Union[str, Sequence[str], None] = 'a7d4f2c81b39'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Годность прокси, известная на момент миграции. Значения перенесены из поля comment,
# которое удалила предыдущая ревизия: там эти пометки вели руками по итогам
# check_proxy.py --sites. Ключ — «хост:порт», естественный ключ строки (id в разных
# окружениях разные, привязываться к ним нельзя).
#
# 194.5.x доходят до mos-sud, но получают от своего провайдера 502 Bad Gateway на
# CONNECT к поддоменам msudrf. 138.249.24.153 — ровно наоборот. 188.143.169.29
# (iparchitect) единственный берёт все три, хотя на msudrf примерно каждый пятый заход
# всё равно отдаёт 502 — на выдачу это не влияет, отказ добирается ретраем.
KNOWN_PORTALS = {
    ("138.249.24.153", 7584): ["msudrf"],
    ("194.5.11.48", 8000): ["mos-sud"],
    ("194.5.10.47", 8000): ["mos-sud"],
    ("194.5.11.31", 8000): ["mos-sud"],
    ("188.143.169.29", 30692): ["mos-sud", "msudrf", "spb"],
}


def upgrade() -> None:
    """Upgrade schema."""
    # Пустой массив, а не NULL: «годность не проверяли» — это пустой набор, и коду не
    # приходится отдельно разбирать NULL.
    op.add_column(
        'proxy',
        sa.Column(
            'portals',
            postgresql.ARRAY(sa.String()),
            server_default=sa.text("'{}'::varchar[]"),
            nullable=False,
        ),
    )

    connection = op.get_bind()
    for (host, port), portals in KNOWN_PORTALS.items():
        connection.execute(
            sa.text(
                "UPDATE proxy SET portals = :portals WHERE host = :host AND port = :port"
            ),
            {"portals": portals, "host": host, "port": port},
        )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('proxy', 'portals')
