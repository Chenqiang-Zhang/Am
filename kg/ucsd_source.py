import re
from collections import Counter
from datetime import date
from typing import Any

from .constants import UCSD_PLAYER_PERCENT
from .files import graph_data_dir, read_gzip_records, write_jsonl
from .ids import hash_id, hash_player_id, make_studio, make_tag

MONTH_NAMES = (
    "January February March April May June July August September October November December".split()
)


def parse_price(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        if value.lower().startswith("free"):
            return 0.0
        dollars = re.search(r"\$([\d.]+)", value)
        if dollars:
            return float(dollars.group(1))
    return None


def parse_release_date(value: Any) -> str | None:
    if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return value
    return None


def parse_posted_date(posted: str) -> str | None:
    match = re.search(r"Posted (\w+) (\d{1,2}), (\d{4})", posted or "")
    if not match or match.group(1) not in MONTH_NAMES:
        return None
    month = MONTH_NAMES.index(match.group(1)) + 1
    return date(int(match.group(3)), month, int(match.group(2))).isoformat()


def parse_votes_up(helpful: str) -> int | None:
    match = re.match(r"(\d+) of", helpful or "")
    return int(match.group(1)) if match else None


def build_game_record(appid: str, metadata: dict[str, Any]) -> dict[str, Any]:
    tags = [make_tag("genre", name, None, None) for name in metadata.get("genres") or []]
    tags += [make_tag("feature", name, None, None) for name in metadata.get("specs") or []]
    tags += [make_tag("usertag", name, None, rank) for rank, name in enumerate(metadata.get("tags") or [], start=1)]
    studios = []
    if metadata.get("developer"):
        studios.append(make_studio(metadata["developer"], "developer"))
    if metadata.get("publisher"):
        studios.append(make_studio(metadata["publisher"], "publisher"))
    return {
        "appid": appid,
        "name": metadata.get("title") or metadata.get("app_name"),
        "name_ja": None,
        "description": None,
        "description_ja": None,
        "release_date": parse_release_date(metadata.get("release_date")),
        "price": parse_price(metadata.get("price")),
        "currency": "USD",
        "tags": tags,
        "studios": studios,
    }


def collect_ucsd(game_count: int) -> None:
    directory = graph_data_dir("ucsd")
    items_path = directory / "australian_users_items.json.gz"

    owner_counts: Counter[str] = Counter()
    for user in read_gzip_records(items_path):
        for item in user.get("items", []):
            owner_counts[item["item_id"]] += 1

    metadata_by_appid = {
        record["id"]: record for record in read_gzip_records(directory / "steam_games.json.gz") if record.get("id")
    }
    target_appids = [appid for appid, _ in owner_counts.most_common() if appid in metadata_by_appid]
    target_appids = target_appids[:game_count]
    target_appid_set = set(target_appids)
    write_jsonl(
        directory / "games.jsonl",
        (build_game_record(appid, metadata_by_appid[appid]) for appid in target_appids),
    )

    play_records = []
    hashed_id_by_raw_id: dict[str, str] = {}
    for user in read_gzip_records(items_path):
        played_items = [
            item
            for item in user.get("items", [])
            if item["item_id"] in target_appid_set and item.get("playtime_forever", 0) > 0
        ]
        player_id = hash_player_id(user["user_id"])
        if not played_items or int(player_id[:8], 16) % 100 >= UCSD_PLAYER_PERCENT:
            continue
        hashed_id_by_raw_id[user["user_id"]] = player_id
        for item in played_items:
            play_records.append(
                {
                    "player_id": player_id,
                    "appid": item["item_id"],
                    "hours": round(item["playtime_forever"] / 60, 2),
                    "hours_2w": round((item.get("playtime_2weeks") or 0) / 60, 2),
                }
            )
    write_jsonl(directory / "played.jsonl", play_records)

    review_records = []
    for user in read_gzip_records(directory / "australian_user_reviews.json.gz"):
        player_id = hashed_id_by_raw_id.get(user["user_id"])
        if player_id is None:
            continue
        for review in user.get("reviews", []):
            if review["item_id"] not in target_appid_set or not review.get("review"):
                continue
            review_records.append(
                {
                    "review_id": hash_id(player_id, review["item_id"], review.get("posted", "")),
                    "appid": review["item_id"],
                    "player_id": player_id,
                    "text": review["review"],
                    "recommended": bool(review.get("recommend")),
                    "votes_up": parse_votes_up(review.get("helpful", "")),
                    "created_at": parse_posted_date(review.get("posted", "")),
                }
            )
    write_jsonl(directory / "reviews.jsonl", review_records)
    print(
        f"ucsd: {len(target_appids)} games, {len(hashed_id_by_raw_id)} players, "
        f"{len(play_records)} played, {len(review_records)} reviews"
    )
