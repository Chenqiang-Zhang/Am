import hashlib
import re
from typing import Any

from utils import settings


def hash_id(*parts: str) -> str:
    return hashlib.sha256(":".join(parts).encode()).hexdigest()[:32]


def to_snake_case(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def hash_player_id(raw_player_id: str) -> str:
    if not settings.PLAYER_ID_SALT:
        raise RuntimeError("PLAYER_ID_SALT is not set")
    return hash_id(settings.PLAYER_ID_SALT, raw_player_id)


def make_tag(kind: str, name: str, name_ja: str | None, rank: int | None) -> dict[str, Any]:
    return {"tag_id": f"{kind}:{to_snake_case(name)}", "kind": kind, "name": name, "name_ja": name_ja, "rank": rank}


def make_studio(name: str, role: str) -> dict[str, str]:
    return {"studio_id": to_snake_case(name), "name": name, "role": role}
