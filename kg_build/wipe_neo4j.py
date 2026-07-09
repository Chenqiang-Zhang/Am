"""
Delete ALL nodes/relationships from the configured Neo4j (Aura) instance.

Batched DETACH DELETE to stay under Aura's per-transaction memory limit.
Reuses the same .env/config.yaml connection resolution as import_kg_to_neo4j.py.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from utils.neo4j_io import connect, load_env_file, resolve_neo4j_conn


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Wipe all data from the Neo4j instance.")
    p.add_argument("--config", type=Path, default=Path("config.yaml"))
    p.add_argument("--uri", default=None)
    p.add_argument("--user", default=None)
    p.add_argument("--password", default=None)
    p.add_argument("--database", default=None)
    p.add_argument("--batch-size", type=int, default=5000)
    p.add_argument("--yes", action="store_true", help="Skip the interactive confirmation prompt.")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    config_dir = args.config.resolve().parent

    load_env_file(config_dir / ".env")
    load_env_file()

    cfg: dict = {}
    if args.config.exists():
        import yaml
        with args.config.open(encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}

    conn = resolve_neo4j_conn(args, cfg)
    uri, user, password, database = conn["uri"], conn["user"], conn["password"], conn["database"]

    driver = connect(uri, user, password)
    try:
        driver.verify_connectivity()
        print(f"Connected to {uri}")

        with driver.session(database=database) as session:
            total = session.run("MATCH (n) RETURN count(n) AS c").single()["c"]
        print(f"Nodes before wipe: {total:,}")

        if not args.yes:
            reply = input(f"Type 'DELETE' to permanently wipe all {total:,} nodes: ")
            if reply.strip() != "DELETE":
                print("Aborted.")
                return

        deleted = 0
        with driver.session(database=database) as session:
            while True:
                result = session.run(
                    "MATCH (n) WITH n LIMIT $batch DETACH DELETE n RETURN count(n) AS c",
                    batch=args.batch_size,
                )
                c = result.single()["c"]
                deleted += c
                if c == 0:
                    break
                print(f"  deleted {deleted:,}/{total:,}...")

        with driver.session(database=database) as session:
            remaining = session.run("MATCH (n) RETURN count(n) AS c").single()["c"]
        print(f"Done. Nodes remaining: {remaining:,}")
    finally:
        driver.close()


if __name__ == "__main__":
    main()
