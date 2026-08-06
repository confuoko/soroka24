"""proxy: пул прокси для походов браузера на портал суда

Revision ID: c9f8b2d40e15
Revises: b7e4a3f19c02
Create Date: 2026-08-06 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c9f8b2d40e15'
down_revision: Union[str, Sequence[str], None] = 'b7e4a3f19c02'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Пул прокси хранится в БД, а не в env: список меняется часто (прокси покупаются
    # и протухают), и править его надо через админку, не передеплоивая воркеры.
    op.create_table(
        "proxy",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("scheme", sa.String(length=16), nullable=False),
        sa.Column("host", sa.String(length=255), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("username", sa.String(length=255), nullable=True),
        sa.Column("password", sa.String(length=255), nullable=True),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("comment", sa.String(length=255), nullable=True),
        # Ключ ротации: NULL = прокси ещё не использовали, такой берётся первым.
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        # host+port — естественный ключ: дважды один и тот же прокси не заводим.
        sa.UniqueConstraint("host", "port", name="uq_proxy_host_port"),
    )
    # Оба индекса под запрос аренды: WHERE enabled ORDER BY last_used_at.
    op.create_index(op.f("ix_proxy_enabled"), "proxy", ["enabled"], unique=False)
    op.create_index(op.f("ix_proxy_last_used_at"), "proxy", ["last_used_at"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_proxy_last_used_at"), table_name="proxy")
    op.drop_index(op.f("ix_proxy_enabled"), table_name="proxy")
    op.drop_table("proxy")
