import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_env() -> None:
    path = ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env()

GRAPH = os.environ.get("GRAPH", "live")
CURRENCY = {"live": "JPY", "ucsd": "USD"}[GRAPH]
PLAYER_ID_SALT = os.environ.get("PLAYER_ID_SALT", "")


def llm_settings(purpose: str) -> dict[str, str | None]:
    prefix = purpose.upper()
    return {
        "provider": os.environ.get(f"{prefix}_LLM_PROVIDER", "gemini"),
        "model": os.environ.get(f"{prefix}_LLM_MODEL") or None,
        "base_url": os.environ.get(f"{prefix}_LLM_BASE_URL") or None,
    }


def neo4j_connection(graph_name: str = GRAPH) -> dict[str, str]:
    prefix = graph_name.upper()
    return {
        "uri": os.environ[f"{prefix}_NEO4J_URI"],
        "user": os.environ.get(f"{prefix}_NEO4J_USER", "neo4j"),
        "password": os.environ[f"{prefix}_NEO4J_PASSWORD"],
        "database": os.environ.get(f"{prefix}_NEO4J_DATABASE", "neo4j"),
    }
