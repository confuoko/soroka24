"""court: починить адреса двух елецких судов Липецкой области

Revision ID: c4e8b7a21f60
Revises: a1c6d3f95e27
Create Date: 2026-08-16 18:20:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c4e8b7a21f60'
down_revision: Union[str, Sequence[str], None] = 'a1c6d3f95e27'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Два елецких суда Липецкой области были записаны с адресом «http://1elez@mirsud.lipetsk.ru»
# и «http://2elez@mirsud.lipetsk.ru». Тут сразу две беды:
#
# * адрес синтаксически битый — из-за «@» всё, что слева, разбирается как имя
#   пользователя, поэтому хост у ОБОИХ выходил один и тот же (mirsud.lipetsk.ru), и
#   get_by_host (app/repositories/courts.py) на них честно отказывался выбирать суд;
# * портал был указан чужой. На самом деле оба суда сидят на том же движке msudrf.ru,
#   что и остальные 62 суда региона: elec-r1.lpk.msudrf.ru и elec-r2.lpk.msudrf.ru
#   (адреса подтверждены на живых карточках дел).
#
# После правки Липецкая область покрыта целиком: домен lpk.msudrf.ru уже отображён на
# MsudrfCourtClient в COURT_BY_DOMAIN. Те же две правки внесены в data/courts.json,
# чтобы sync_courts не вернул старые значения обратно.
LIPETSK_URL_FIXES = {
    "48MS0012": "https://elec-r1.lpk.msudrf.ru",  # Елецкий районный участок № 1
    "48MS0013": "https://elec-r2.lpk.msudrf.ru",  # Елецкий районный участок № 2
}


def upgrade() -> None:
    """Upgrade schema."""
    connection = op.get_bind()

    for code, base_url in LIPETSK_URL_FIXES.items():
        connection.execute(
            sa.text("UPDATE court SET base_url = :base_url WHERE code = :code"),
            {"base_url": base_url, "code": code},
        )

    # Та же проверка, что в b3f7c21a9e04: у судов msudrf.ru поддомен свой на каждый
    # участок, делить его двоим нельзя — иначе суд по ссылке не определить.
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
    # Ничего не возвращаем: побитые адреса были ошибкой данных, а не состоянием схемы.
    pass
