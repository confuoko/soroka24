"""search_task: вход по ссылке (source_url), uid становится необязательным

Revision ID: d1a6c3e802f7
Revises: c9f8b2d40e15
Create Date: 2026-08-06 18:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd1a6c3e802f7'
down_revision: Union[str, Sequence[str], None] = 'c9f8b2d40e15'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Дела с порталов msudrf.ru приходят ссылкой: поиска по УИД там нет, зато карточка
    # открывается по прямому адресу. УИД становится известен только после похода на
    # страницу, поэтому задачу приходится заводить без него.
    op.add_column("search_task", sa.Column("source_url", sa.String(), nullable=True))
    op.create_index(op.f("ix_search_task_source_url"), "search_task", ["source_url"])
    op.alter_column("search_task", "uid", existing_type=sa.String(), nullable=True)
    # Но пустыми не могут быть оба сразу: по такой задаче нечего делать.
    op.create_check_constraint(
        "ck_search_task_uid_or_url", "search_task", "uid IS NOT NULL OR source_url IS NOT NULL"
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint("ck_search_task_uid_or_url", "search_task", type_="check")
    # Вернуть NOT NULL можно только выкинув задачи, заведённые по ссылке: УИД у них
    # мог остаться пустым (например, портал так и не открылся).
    op.execute("DELETE FROM search_task WHERE uid IS NULL")
    op.alter_column("search_task", "uid", existing_type=sa.String(), nullable=False)
    op.drop_index(op.f("ix_search_task_source_url"), table_name="search_task")
    op.drop_column("search_task", "source_url")
