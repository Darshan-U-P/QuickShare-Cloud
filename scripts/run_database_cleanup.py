"""Run one bounded pass of the PostgreSQL file-cleanup worker."""
from __future__ import annotations

import argparse
import json
import sys

from services import database
from services.database_cleanup import process_cleanup_jobs


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=20, help="Maximum jobs to claim.")
    args = parser.parse_args()
    if not database.is_configured():
        print("DATABASE_URL is not set.", file=sys.stderr)
        return 2
    try:
        report = process_cleanup_jobs(max_jobs=args.limit)
    except database.DatabaseError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps({"ok": True, **report}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
