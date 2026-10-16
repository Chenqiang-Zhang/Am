import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .constants import MAX_GAME_COUNT
from .files import graph_data_dir, read_jsonl, write_jsonl
from .http_client import get_json
from .ids import hash_player_id, make_studio, make_tag

STEAM_API = "https://api.steampowered.com"
STEAM_STORE = "https://store.steampowered.com"
STORE_ITEM_BATCH_SIZE = 93
QUERY_PAGE_SIZE = 1000
USER_TAGS_PER_GAME = 50
HELPFUL_REVIEW_COUNT = 15
RECENT_REVIEW_COUNT = 5
REVIEW_PAUSE_SECONDS = 0.5
APP_DETAILS_PAUSE_SECONDS = 0.3
STORE_ITEM_DATA_REQUEST = {
    "include_basic_info": True,
    "include_release": True,
    "include_tag_count": USER_TAGS_PER_GAME,
    "include_reviews": True,
    "include_all_purchase_options": True,
}


def iso_date_from_timestamp(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).date().isoformat()


def fetch_store_items(appids: list[str], language: str) -> dict[str, dict[str, Any]]:
    payload = {
        "ids": [{"appid": int(appid)} for appid in appids],
        "context": {"language": language, "country_code": "JP"},
        "data_request": STORE_ITEM_DATA_REQUEST,
    }
    response = get_json(f"{STEAM_API}/IStoreBrowseService/GetItems/v1", {"input_json": json.dumps(payload)})
    return {str(item["appid"]): item for item in response["response"].get("store_items", []) if item.get("name")}


def list_game_appids() -> list[str]:
    appids: list[str] = []
    while True:
        payload = {
            "query": {
                "start": len(appids),
                "count": QUERY_PAGE_SIZE,
                "filters": {"type_filters": {"include_games": True}},
            },
            "context": {"language": "english", "country_code": "US"},
            "data_request": {},
        }
        response = get_json(f"{STEAM_API}/IStoreQueryService/Query/v1", {"input_json": json.dumps(payload)})
        result = response["response"]
        store_items = result.get("store_items", [])
        if not store_items:
            break
        appids += [str(item["appid"]) for item in store_items]
        if len(appids) >= result["metadata"]["total_matching_records"]:
            break
    return appids


def select_top_games(directory: Path) -> list[str]:
    selection_path = directory / "appids.json"
    if not selection_path.exists():
        appids = list_game_appids()
        review_counts = []
        for start in range(0, len(appids), STORE_ITEM_BATCH_SIZE):
            batch = appids[start : start + STORE_ITEM_BATCH_SIZE]
            for appid, item in fetch_store_items(batch, "english").items():
                summary = item.get("reviews", {}).get("summary_filtered", {})
                review_counts.append({"appid": appid, "review_count": summary.get("review_count", 0)})
            print(f"live: counted reviews {start + len(batch)}/{len(appids)}")
        review_counts.sort(key=lambda entry: entry["review_count"], reverse=True)
        selection_path.write_text(json.dumps(review_counts[:MAX_GAME_COUNT]))
    return [entry["appid"] for entry in json.loads(selection_path.read_text())]


def fetch_tag_names(language: str) -> dict[int, str]:
    response = get_json(f"{STEAM_API}/IStoreService/GetTagList/v1/", {"language": language})
    return {tag["tagid"]: tag["name"] for tag in response["response"]["tags"]}


def fetch_category_names_ja() -> dict[int, str]:
    response = get_json(f"{STEAM_API}/IStoreBrowseService/GetStoreCategories/v1", {"language": "japanese"})
    return {category["categoryid"]: category["display_name"] for category in response["response"]["categories"]}


def fetch_app_details(appids: list[str], cache_path: Path) -> dict[str, dict[str, Any]]:
    details_by_appid = {record["appid"]: record for record in read_jsonl(cache_path)}
    for position, appid in enumerate(appids, start=1):
        if appid in details_by_appid:
            continue
        response = get_json(
            f"{STEAM_STORE}/api/appdetails",
            {"appids": appid, "l": "english", "cc": "jp"},
            pause_seconds=APP_DETAILS_PAUSE_SECONDS,
        )[appid]
        data = response.get("data", {}) if response.get("success") else {}
        details = {
            "appid": appid,
            "genres": [genre["description"] for genre in data.get("genres", [])],
            "categories": [
                {"id": category["id"], "name": category["description"]} for category in data.get("categories", [])
            ],
            "is_free": bool(data.get("is_free")),
        }
        write_jsonl(cache_path, [details], append=True)
        details_by_appid[appid] = details
        if position % 100 == 0:
            print(f"live: appdetails {position}/{len(appids)}")
    return details_by_appid


def parse_price(details: dict[str, Any], store_item: dict[str, Any]) -> float | None:
    if details["is_free"]:
        return 0.0
    purchase_option = store_item.get("best_purchase_option", {})
    if "final_price_in_cents" in purchase_option:
        return int(purchase_option["final_price_in_cents"]) / 100
    return None


def collect_games(appids: list[str], directory: Path) -> None:
    tag_names_en, tag_names_ja = fetch_tag_names("english"), fetch_tag_names("japanese")
    tag_name_ja_by_en = {tag_names_en[tag_id]: tag_names_ja[tag_id] for tag_id in tag_names_en if tag_id in tag_names_ja}
    category_names_ja = fetch_category_names_ja()
    details_by_appid = fetch_app_details(appids, directory / "appdetails.jsonl")

    games = []
    for start in range(0, len(appids), STORE_ITEM_BATCH_SIZE):
        batch = appids[start : start + STORE_ITEM_BATCH_SIZE]
        items_en = fetch_store_items(batch, "english")
        items_ja = fetch_store_items(batch, "japanese")
        for appid in batch:
            if appid not in items_en:
                continue
            item_en, item_ja, details = items_en[appid], items_ja.get(appid, {}), details_by_appid[appid]
            basic_en, basic_ja = item_en.get("basic_info", {}), item_ja.get("basic_info", {})

            tags = [make_tag("genre", genre, tag_name_ja_by_en.get(genre), None) for genre in details["genres"]]
            tags += [
                make_tag("feature", category["name"], category_names_ja.get(category["id"]), None)
                for category in details["categories"]
            ]
            user_tags = sorted(item_en.get("tags", []), key=lambda tag: tag["weight"], reverse=True)
            known_user_tags = [tag for tag in user_tags if tag["tagid"] in tag_names_en]
            tags += [
                make_tag("usertag", tag_names_en[tag["tagid"]], tag_names_ja.get(tag["tagid"]), rank)
                for rank, tag in enumerate(known_user_tags, start=1)
            ]

            studios = [make_studio(developer["name"], "developer") for developer in basic_en.get("developers", [])]
            studios += [make_studio(publisher["name"], "publisher") for publisher in basic_en.get("publishers", [])]
            release_timestamp = item_en.get("release", {}).get("steam_release_date")
            games.append(
                {
                    "appid": appid,
                    "name": item_en["name"],
                    "name_ja": item_ja.get("name"),
                    "description": basic_en.get("short_description"),
                    "description_ja": basic_ja.get("short_description"),
                    "release_date": iso_date_from_timestamp(release_timestamp) if release_timestamp else None,
                    "price": parse_price(details, item_en),
                    "currency": "JPY",
                    "tags": tags,
                    "studios": studios,
                }
            )
    write_jsonl(directory / "games.jsonl", games)
    print(f"live: {len(games)} games")


def collect_reviews(appids: list[str], directory: Path) -> None:
    done_path = directory / "reviews_done.txt"
    done_appids = set(done_path.read_text().split()) if done_path.exists() else set()
    for position, appid in enumerate(appids, start=1):
        if appid in done_appids:
            continue
        reviews_by_id: dict[str, dict[str, Any]] = {}
        for review_filter, count in (("all", HELPFUL_REVIEW_COUNT), ("recent", RECENT_REVIEW_COUNT)):
            response = get_json(
                f"{STEAM_STORE}/appreviews/{appid}",
                {
                    "json": 1,
                    "filter": review_filter,
                    "language": "english",
                    "num_per_page": count,
                    "purchase_type": "all",
                    "review_type": "all",
                },
                pause_seconds=REVIEW_PAUSE_SECONDS,
            )
            for review in response.get("reviews", []):
                if review.get("review"):
                    reviews_by_id[review["recommendationid"]] = review

        review_records, play_records = [], []
        for review in reviews_by_id.values():
            author = review["author"]
            player_id = hash_player_id(author["steamid"])
            review_records.append(
                {
                    "review_id": review["recommendationid"],
                    "appid": appid,
                    "player_id": player_id,
                    "text": review["review"],
                    "recommended": bool(review["voted_up"]),
                    "votes_up": review.get("votes_up"),
                    "created_at": iso_date_from_timestamp(review["timestamp_created"]),
                }
            )
            if author.get("playtime_forever", 0) > 0:
                play_records.append(
                    {
                        "player_id": player_id,
                        "appid": appid,
                        "hours": round(author["playtime_forever"] / 60, 2),
                        "hours_2w": round(author.get("playtime_last_two_weeks", 0) / 60, 2),
                    }
                )
        write_jsonl(directory / "reviews.jsonl", review_records, append=True)
        write_jsonl(directory / "played.jsonl", play_records, append=True)
        with done_path.open("a") as done_file:
            done_file.write(appid + "\n")
        if position % 50 == 0:
            print(f"live: reviews {position}/{len(appids)}")


def collect_live(game_count: int) -> None:
    directory = graph_data_dir("live")
    appids = select_top_games(directory)[:game_count]
    collect_games(appids, directory)
    collect_reviews(appids, directory)
