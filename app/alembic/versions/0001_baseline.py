"""baseline: the database as it is today

Your tables already exist (created by the app on startup), so this first step does nothing.
You mark the database with it once (`alembic stamp head`) and every later change builds on it.

Revision ID: 0001_baseline
Revises:
Create Date: 2026-10-26
"""
revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
