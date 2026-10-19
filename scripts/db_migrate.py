"""Apply PostgreSQL schema migrations for QuickShare Cloud."""
from __future__ import annotations

import json
import sys

from services import database


def main() -> int:
    if not database.is_configured():
        print("DATABASE_URL is not set. Add a managed PostgreSQL connection URI first.", file=sys.stderr)
        return 2
    try:
        applied = database.run_migrations()
        healthy = database.health_check()
    except database.DatabaseError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps({
        "ok": healthy,
        "database": "postgresql",
        "applied_migrations": applied,
        "message": "Schema is ready. The application still uses the legacy metadata backend until cutover.",
    }, indent=2))
    return 0 if healthy else 1


if __name__ == "__main__":
    raise SystemExit(main())
