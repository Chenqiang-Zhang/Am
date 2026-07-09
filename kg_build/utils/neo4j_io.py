"""
Shared Neo4j connection helpers used by every script that talks to Neo4j
directly over Bolt (import_kg_to_neo4j.py, wipe_neo4j.py, enrich_products.py).

.env loading itself lives in llm_client.py (re-exported here) so there is a
single canonical implementation shared by both the LLM- and Neo4j-facing
scripts.
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import Any

from utils.llm_client import load_env_file  # noqa: F401  (re-exported for callers of this module)


def resolve_neo4j_conn(args: argparse.Namespace, cfg: dict[str, Any]) -> dict[str, str | None]:
    """Resolve uri/user/password/database with precedence: CLI arg > env var > config.yaml."""
    neo4j_cfg = cfg.get("neo4j", {})
    return {
        "uri": getattr(args, "uri", None) or neo4j_cfg.get("uri") or os.environ.get("NEO4J_URI", ""),
        "user": getattr(args, "user", None) or os.environ.get("NEO4J_USERNAME") or neo4j_cfg.get("username", "neo4j"),
        "password": getattr(args, "password", None) or os.environ.get("NEO4J_PASSWORD") or neo4j_cfg.get("password", ""),
        "database": getattr(args, "database", None) or os.environ.get("NEO4J_DATABASE") or neo4j_cfg.get("database"),
    }


def connect(uri: str, user: str, password: str) -> Any:
    """Validate connection info, import the neo4j driver, and build it.
    Exits the process with a helpful message if either step fails."""
    if not uri or not password:
        print(
            "Neo4j URI or password not found.\n"
            "  Set NEO4J_PASSWORD in Am/.env and neo4j.uri in config.yaml\n"
            "  or pass --uri / --password.",
            file=sys.stderr,
        )
        sys.exit(2)
    try:
        from neo4j import GraphDatabase
    except ImportError:
        print("neo4j package not installed. Run: pip install -r requirements.txt", file=sys.stderr)
        sys.exit(2)
    return GraphDatabase.driver(uri, auth=(user, password))
