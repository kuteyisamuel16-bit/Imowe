"""
backup_db.py - saves a copy of every table in your database to one .zip file.

Run it from the repo root, on your own computer, BEFORE any risky change:

    python backup_db.py

It uses the same database connection as the app (so DATABASE_URL must be set the same way it is
for the app). The zip holds one file per table (one JSON record per line) plus a small info file.
Keep it somewhere safe (not in the repo). It contains student data, so treat it like a password.

If your database provider has its own "backup" or "snapshot" button, use that too. This script is
a second, provider-independent copy that you can read with any tool.
"""
import json
import sys
import zipfile
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import inspect, text

from app.database import engine


def _default(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    return str(value)


def main() -> int:
    stamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    path = f"imowe-backup-{stamp}.zip"
    tables = sorted(inspect(engine).get_table_names())
    total = 0
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z, engine.connect() as conn:
        for name in tables:
            quoted = '"' + name.replace('"', '""') + '"'
            result = conn.execute(text(f"SELECT * FROM {quoted}"))
            cols = list(result.keys())
            count = 0
            lines = []
            for row in result:
                lines.append(json.dumps(dict(zip(cols, row)), default=_default, ensure_ascii=False))
                count += 1
            z.writestr(f"{name}.jsonl", "\n".join(lines))
            print(f"  {name}: {count} rows")
            total += count
        z.writestr("INFO.txt", f"IMOWE backup\nCreated (UTC): {datetime.utcnow().isoformat()}\nTables: {len(tables)}\nRows: {total}\n")
    print(f"\nSaved {total} rows from {len(tables)} tables to {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
