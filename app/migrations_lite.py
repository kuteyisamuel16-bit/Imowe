"""
Ad-hoc, idempotent schema patches for an app that isn't using Alembic in its
deploy pipeline yet. Each statement is safe to run every single startup -
IF NOT EXISTS / DROP NOT NULL make repeats no-ops once applied once.
"""
import logging
from sqlalchemy import text

logger = logging.getLogger("imowe.migrations")

STATEMENTS = [
    "ALTER TABLE materials ADD COLUMN IF NOT EXISTS extracted_text TEXT",
    "ALTER TABLE materials ADD COLUMN IF NOT EXISTS material_type VARCHAR(20) DEFAULT 'document'",
    "ALTER TABLE materials ADD COLUMN IF NOT EXISTS linked_material_id UUID REFERENCES materials(id)",
    "ALTER TABLE materials ALTER COLUMN file_path DROP NOT NULL",
    "ALTER TABLE ai_interactions ADD COLUMN IF NOT EXISTS material_id UUID REFERENCES materials(id)",
]


def run(engine):
    with engine.begin() as conn:
        for stmt in STATEMENTS:
            try:
                conn.execute(text(stmt))
            except Exception as e:
                logger.warning(f"Migration statement skipped: {stmt} -> {e}")
