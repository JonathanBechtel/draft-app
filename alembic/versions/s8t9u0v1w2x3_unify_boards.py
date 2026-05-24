"""Unify big_boards + (future) mock_drafts into a single boards schema.

Revision ID: s8t9u0v1w2x3
Revises: r7s8t9u0v1w2
Create Date: 2026-05-24

Rationale: see docs/consensus_mock_plan.md ("Big Board and Mock Draft —
One Unified Schema"). After building the big-board path end-to-end we
realized the data shapes overlap enough that a single Board table with
a kind discriminator + a few nullable mock-only columns is cleaner than
two parallel tables.

This migration:
  - Renames big_boards -> boards, big_board_entries -> board_entries
  - Renames the entry "rank" column to "position" (works for both rank
    and pick_number)
  - Adds the board_kind_enum and a kind column (default BIG_BOARD so
    existing rows are tagged correctly)
  - Adds num_rounds on boards (nullable; required for MOCK_DRAFT)
  - Adds round, team_id, original_team_id, trade_note on board_entries
    (all nullable; populated only for MOCK_DRAFT rows)
  - Adds a CHECK constraint enforcing kind-specific null/non-null shape
  - Renames the matching indexes and unique constraints

Existing big_boards rows are preserved as-is; the default on the kind
column tags them BIG_BOARD so consensus and admin queries continue
working without any data migration.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op  # type: ignore[attr-defined]
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "s8t9u0v1w2x3"
down_revision: Union[str, None] = "r7s8t9u0v1w2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


KIND_ENUM_NAME = "board_kind_enum"
KIND_VALUES = ("BIG_BOARD", "MOCK_DRAFT")


def upgrade() -> None:
    # 1. Create the board_kind enum.
    kind_enum = postgresql.ENUM(*KIND_VALUES, name=KIND_ENUM_NAME, create_type=False)
    kind_enum.create(op.get_bind(), checkfirst=True)

    # 2. Rename the tables.
    op.rename_table("big_boards", "boards")
    op.rename_table("big_board_entries", "board_entries")

    # 3. Rename the entry "rank" column to "position".
    op.alter_column("board_entries", "rank", new_column_name="position")

    # 4. Add kind discriminator + num_rounds to boards.
    op.add_column(
        "boards",
        sa.Column(
            "kind",
            kind_enum,
            nullable=False,
            server_default="BIG_BOARD",
        ),
    )
    op.add_column("boards", sa.Column("num_rounds", sa.Integer(), nullable=True))
    op.create_index("ix_boards_kind", "boards", ["kind"])
    op.create_index("ix_boards_kind_draft_year", "boards", ["kind", "draft_year"])

    # 5. Rename board_size to size on boards for kind-agnostic clarity.
    op.alter_column("boards", "board_size", new_column_name="size")

    # 6. Add the kind-specific CHECK constraint.
    op.create_check_constraint(
        "ck_boards_kind_num_rounds",
        "boards",
        "(kind = 'BIG_BOARD' AND num_rounds IS NULL) "
        "OR (kind = 'MOCK_DRAFT' AND num_rounds IS NOT NULL)",
    )

    # 7. Add mock-only columns to board_entries.
    op.add_column("board_entries", sa.Column("round", sa.Integer(), nullable=True))
    op.add_column(
        "board_entries", sa.Column("team_id", sa.Integer(), nullable=True)
    )
    op.add_column(
        "board_entries", sa.Column("original_team_id", sa.Integer(), nullable=True)
    )
    op.add_column(
        "board_entries", sa.Column("trade_note", sa.String(), nullable=True)
    )
    op.create_foreign_key(
        "fk_board_entries_team",
        "board_entries",
        "nba_teams",
        ["team_id"],
        ["id"],
    )
    op.create_foreign_key(
        "fk_board_entries_original_team",
        "board_entries",
        "nba_teams",
        ["original_team_id"],
        ["id"],
    )

    # 8. Rename indexes that carry the old table prefix.
    op.execute("ALTER INDEX ix_big_boards_status_draft_year RENAME TO ix_boards_status_draft_year")
    op.execute("ALTER INDEX ix_big_boards_source_draft_year RENAME TO ix_boards_source_draft_year")
    op.execute("ALTER INDEX ix_big_boards_news_source_id RENAME TO ix_boards_news_source_id")
    op.execute("ALTER INDEX ix_big_boards_draft_year RENAME TO ix_boards_draft_year")
    op.execute("ALTER INDEX ix_big_boards_published_at RENAME TO ix_boards_published_at")
    op.execute("ALTER INDEX ix_big_boards_status RENAME TO ix_boards_status")
    op.execute("ALTER INDEX ix_big_board_entries_board_id RENAME TO ix_board_entries_board_id")
    op.execute("ALTER INDEX ix_big_board_entries_player RENAME TO ix_board_entries_player")

    # 9. Rename unique constraints.
    op.execute(
        "ALTER TABLE board_entries RENAME CONSTRAINT "
        "uq_big_board_entries_board_rank TO uq_board_entries_board_position"
    )
    op.execute(
        "ALTER TABLE board_entries RENAME CONSTRAINT "
        "uq_big_board_entries_board_player TO uq_board_entries_board_player"
    )

    # FK constraint names are cosmetic and vary between environments
    # bootstrapped via SQLModel.metadata.create_all (Postgres-default
    # names like `big_boards_news_source_id_fkey`) vs alembic upgrade
    # from base (explicit names from the original migration). The FK
    # itself still enforces the relationship regardless of its label,
    # so leaving the existing constraint names in place avoids guessing
    # which environment we're hitting.

    # Note: the kind column keeps its server_default = BIG_BOARD so
    # existing Board(...) constructor calls (which don't pass kind=
    # explicitly because big-board admin entry is the only kind that
    # has a UI today) continue to insert successfully. When the
    # mock-draft entry path lands and starts setting kind explicitly,
    # we can revisit dropping the default in a follow-up.

    # 11. Rename any existing auth_dataset_permission rows pointing at
    # the old "big_boards" dataset name. KNOWN_DATASETS in the service
    # layer was renamed to "boards", so without this step any worker
    # user who had explicit big-board permissions would be silently
    # locked out of /admin/boards after deploy.
    op.execute(
        "UPDATE auth_dataset_permission SET dataset = 'boards' "
        "WHERE dataset = 'big_boards'"
    )


def downgrade() -> None:
    # Reverse the renames + drops in inverse order.
    # (The kind column's server_default was kept on through upgrade, so
    # nothing to restore here.)

    # Restore the old "big_boards" dataset name on any permission rows
    # so a downgrade leaves worker auth in the pre-rename state.
    op.execute(
        "UPDATE auth_dataset_permission SET dataset = 'big_boards' "
        "WHERE dataset = 'boards'"
    )

    # FK constraint names were not renamed during upgrade (see comment
    # in upgrade() for rationale), so nothing to revert here.

    op.execute(
        "ALTER TABLE board_entries RENAME CONSTRAINT "
        "uq_board_entries_board_player TO uq_big_board_entries_board_player"
    )
    op.execute(
        "ALTER TABLE board_entries RENAME CONSTRAINT "
        "uq_board_entries_board_position TO uq_big_board_entries_board_rank"
    )

    op.execute("ALTER INDEX ix_board_entries_player RENAME TO ix_big_board_entries_player")
    op.execute("ALTER INDEX ix_board_entries_board_id RENAME TO ix_big_board_entries_board_id")
    op.execute("ALTER INDEX ix_boards_status RENAME TO ix_big_boards_status")
    op.execute("ALTER INDEX ix_boards_published_at RENAME TO ix_big_boards_published_at")
    op.execute("ALTER INDEX ix_boards_draft_year RENAME TO ix_big_boards_draft_year")
    op.execute("ALTER INDEX ix_boards_news_source_id RENAME TO ix_big_boards_news_source_id")
    op.execute("ALTER INDEX ix_boards_source_draft_year RENAME TO ix_big_boards_source_draft_year")
    op.execute("ALTER INDEX ix_boards_status_draft_year RENAME TO ix_big_boards_status_draft_year")

    op.drop_constraint(
        "fk_board_entries_original_team", "board_entries", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_board_entries_team", "board_entries", type_="foreignkey"
    )
    op.drop_column("board_entries", "trade_note")
    op.drop_column("board_entries", "original_team_id")
    op.drop_column("board_entries", "team_id")
    op.drop_column("board_entries", "round")

    op.drop_constraint("ck_boards_kind_num_rounds", "boards", type_="check")
    op.alter_column("boards", "size", new_column_name="board_size")
    op.drop_index("ix_boards_kind_draft_year", table_name="boards")
    op.drop_index("ix_boards_kind", table_name="boards")
    op.drop_column("boards", "num_rounds")
    op.drop_column("boards", "kind")

    op.alter_column("board_entries", "position", new_column_name="rank")
    op.rename_table("board_entries", "big_board_entries")
    op.rename_table("boards", "big_boards")

    bind = op.get_bind()
    sa.Enum(name=KIND_ENUM_NAME).drop(bind, checkfirst=True)
