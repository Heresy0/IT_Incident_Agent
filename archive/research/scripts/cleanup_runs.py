"""Explicit cleanup of terminal run history; never deletes checkpoint/knowledge tables."""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from local_run import load_local_env


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=14)
    args = parser.parse_args()
    if not 1 <= args.days <= 365:
        parser.error("days must be between 1 and 365")
    load_local_env()
    from backend.config.incident import IncidentConfig
    from backend.service.run_store import PostgresRunStore
    store = PostgresRunStore(IncidentConfig.from_file(ROOT / 'app' / 'config.json').postgres_dsn)
    try:
        print(f"Deleted terminal run records: {store.cleanup(args.days)}")
    finally:
        store.close()


if __name__ == "__main__":
    main()
