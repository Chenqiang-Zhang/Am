"""A tiny synthetic Steam graph and an in-memory stand-in for GraphReader.

Each FakeGraphReader method answers what the Cypher of the same name in utils/graph_reader.py
answers, written again in plain Python, so the recommenders can be tested without Neo4j.
tests/test_cypher_integration.py loads the same graph into a real Neo4j and checks that the
Cypher agrees with these implementations.
"""

import math
import random
from collections import defaultdict
from pathlib import Path
from typing import Any

from kg.aspect_extraction import SEED_ASPECTS
from kg.files import write_jsonl
from kg.ids import make_studio, make_tag
from utils.graph_reader import TAG_MAX_RANK

# (kind, name, rank). rank is only set for user tags, as in kg/ucsd_source.py.
GAMES: list[dict[str, Any]] = [
    {
        "appid": "10",
        "name": "Alpha",
        "price": 9.99,
        "tags": [("genre", "Action", None), ("usertag", "Action", 1), ("usertag", "Shooter", 2), ("feature", "Single-player", None)],
        "studios": [("Valve", "developer"), ("Valve", "publisher")],
    },
    {
        "appid": "20",
        "name": "Bravo",
        "price": 4.99,
        "tags": [
            ("genre", "Action", None),
            ("genre", "Indie", None),
            ("usertag", "Indie", 1),
            ("usertag", "Puzzle", 2),
            ("feature", "Co-op", None),
        ],
        "studios": [("Indie Co", "developer"), ("Big Pub", "publisher")],
    },
    {
        "appid": "30",
        "name": "Charlie",
        "price": 19.99,
        "tags": [
            ("genre", "RPG", None),
            ("usertag", "RPG", 1),
            ("usertag", "Open World", 2),
            ("usertag", "Action", 11),
            ("feature", "Single-player", None),
        ],
        "studios": [("RPG House", "developer"), ("Big Pub", "publisher")],
    },
    {
        "appid": "40",
        "name": "Delta",
        "price": 0.0,
        "tags": [("usertag", "Puzzle", 1), ("usertag", "Indie", 2), ("genre", "Indie", None)],
        "studios": [("Indie Co", "developer"), ("Indie Co", "publisher")],
    },
    {
        "appid": "50",
        "name": "Echo",
        "price": 0.0,
        "tags": [
            ("genre", "Action", None),
            ("genre", "Free To Play", None),  # Steam's genre spelling; the user tag is "Free to Play"
            ("usertag", "Shooter", 1),
            ("usertag", "Multiplayer", 2),
            ("feature", "Online PvP", None),
        ],
        "studios": [("Valve", "developer"), ("Valve", "publisher")],
    },
    {
        "appid": "60",
        "name": "Foxtrot",
        "price": 29.99,
        "tags": [
            ("genre", "RPG", None),
            ("usertag", "Open World", 1),
            ("usertag", "RPG", 2),
            ("usertag", "Action", 3),
            ("usertag", "Free to Play", 4),
        ],
        "studios": [("RPG House", "developer"), ("RPG House", "publisher")],
    },
    {
        "appid": "70",
        "name": "Golf",
        "price": 14.99,
        "tags": [("genre", "Simulation", None), ("usertag", "Simulation", 1), ("usertag", "Relaxing", 2)],
        "studios": [("Sim Studio", "developer"), ("Big Pub", "publisher")],
    },
    {
        "appid": "80",
        "name": "Hotel",
        "price": 24.99,
        "tags": [("genre", "Strategy", None), ("usertag", "Strategy", 1), ("feature", "Single-player", None)],
        "studios": [("Strat Inc", "developer"), ("Strat Inc", "publisher")],
    },
    {
        "appid": "90",
        "name": "India",
        "price": 2.99,
        "tags": [
            ("genre", "Action", None),
            ("genre", "Indie", None),
            ("genre", "RPG", None),
            ("genre", "Strategy", None),
            ("usertag", "Action", 1),
            ("usertag", "Indie", 2),
            ("usertag", "RPG", 3),
            ("usertag", "Strategy", 4),
            ("usertag", "Puzzle", 5),
            ("usertag", "Shooter", 6),
            ("usertag", "Open World", 7),
            ("usertag", "Relaxing", 8),
            ("usertag", "Simulation", 9),
            ("usertag", "Multiplayer", 10),
            ("usertag", "Casual", 11),
        ],
        "studios": [("Tiny Dev", "developer"), ("Big Pub", "publisher")],
    },
    {
        "appid": "100",
        "name": "Juliet",
        "price": 0.99,
        "tags": [("genre", "Casual", None)],
        "studios": [("Tiny Dev", "developer")],
    },
    {
        "appid": "110",
        "name": "Kilo",
        "price": 39.99,
        "tags": [("genre", "Action", None), ("usertag", "Action", 1)],
        "studios": [("Big Pub", "developer"), ("Big Pub", "publisher")],
    },
    {
        "appid": "120",
        "name": "Lima",
        "price": None,
        "tags": [("feature", "Single-player", None)],
        "studios": [("Lima Dev", "developer")],
    },
    {
        "appid": "130",
        "name": "Mike",
        "price": 7.99,
        "tags": [("genre", "Indie", None), ("usertag", "Puzzle", 1), ("usertag", "Indie", 2)],
        "studios": [("Indie Co", "developer"), ("Big Pub", "publisher")],
    },
]

# (player_id, appid, hours, hours_2w)
PLAYS: list[tuple[str, str, float, float]] = [
    ("u1", "10", 100.0, 2.0),
    ("u1", "20", 20.0, 0.0),
    ("u1", "30", 5.0, 0.0),
    ("u1", "70", 0.5, 0.5),
    ("p1", "10", 50.0, 0.0),
    ("p1", "50", 30.0, 0.0),
    ("p1", "20", 10.0, 0.0),
    ("p2", "10", 5.0, 0.0),
    ("p2", "50", 2.0, 0.0),
    ("p2", "60", 40.0, 0.0),
    ("p2", "30", 12.0, 0.0),
    ("p3", "20", 3.0, 0.0),
    ("p3", "40", 8.0, 0.0),
    ("p3", "90", 1.0, 0.0),
    ("p3", "130", 2.0, 0.0),
    ("p4", "30", 60.0, 0.0),
    ("p4", "60", 25.0, 0.0),
    ("p4", "80", 4.0, 0.0),
    ("p5", "10", 1.0, 0.0),
    ("p5", "30", 2.0, 0.0),
    ("p5", "110", 3.0, 0.0),
    ("p6", "70", 10.0, 0.0),
    ("p6", "80", 2.0, 0.0),
    ("p6", "90", 5.0, 0.0),
    ("p6", "110", 1.0, 0.0),
    ("p7", "40", 4.0, 0.0),
    ("solo", "50", 9.0, 0.0),
]

# (review_id, player_id, appid, text, recommended, votes_up, created_at, aspects)
REVIEWS: list[tuple[str, str, str, str, bool, int, str, list[str]]] = [
    ("r1", "u1", "10", "Great shooter to play with friends, very fun", True, 10, "2015-01-01", ["gameplay", "multiplayer"]),
    ("r2", "p1", "10", "A classic that still holds up today", True, 5, "2015-02-01", ["gameplay"]),
    ("r3", "p2", "60", "Huge open world and a long story", True, 3, "2015-03-01", ["story", "content_volume"]),
    ("r4", "p4", "30", "Too buggy at launch but it improved", False, 1, "2015-04-01", ["stability"]),
    ("r5", "u1", "30", "Loved the characters in this RPG", True, 2, "2015-05-01", ["characters"]),
]

PLAYER_IDS = sorted({player_id for player_id, _, _, _ in PLAYS})


def tag_ja(name: str) -> str | None:
    return {"Action": "アクション", "Puzzle": "パズル", "RPG": "RPG"}.get(name)


def tag_id(kind: str, name: str) -> str:
    return make_tag(kind, name, None, None)["tag_id"]


def studio_id(name: str) -> str:
    return make_studio(name, "developer")["studio_id"]


def write_loader_input(directory: Path) -> None:
    """Write the fixture as the JSONL files kg.neo4j_loader.load_graph reads."""
    write_jsonl(
        directory / "games.jsonl",
        (
            {
                "appid": game["appid"],
                "name": game["name"],
                "name_ja": None,
                "description": f"{game['name']} description",
                "description_ja": None,
                "release_date": None,
                "price": game["price"],
                "currency": "USD",
                "tags": [make_tag(kind, name, tag_ja(name), rank) for kind, name, rank in game["tags"]],
                "studios": [make_studio(name, role) for name, role in game["studios"]],
            }
            for game in GAMES
        ),
    )
    write_jsonl(
        directory / "played.jsonl",
        (
            {"player_id": player_id, "appid": appid, "hours": hours, "hours_2w": hours_2w}
            for player_id, appid, hours, hours_2w in PLAYS
        ),
    )
    write_jsonl(
        directory / "reviews.jsonl",
        (
            {
                "review_id": review_id,
                "appid": appid,
                "player_id": player_id,
                "text": text,
                "recommended": recommended,
                "votes_up": votes_up,
                "created_at": created_at,
            }
            for review_id, player_id, appid, text, recommended, votes_up, created_at, _ in REVIEWS
        ),
    )
    write_jsonl(
        directory / "mentions.jsonl",
        ({"review_id": review[0], "aspects": review[7]} for review in REVIEWS),
    )


class FakeGraphReader:
    """In-memory GraphReader. `shuffle_seed` shuffles the rows of every answer, the way a database
    may return equally ranked rows in any order; the recommenders must not depend on that order."""

    def __init__(self, plays=None, shuffle_seed: int | None = None) -> None:
        self.games = {game["appid"]: game for game in GAMES}
        self.plays = list(PLAYS if plays is None else plays)
        self.random = random.Random(shuffle_seed) if shuffle_seed is not None else None
        self.calls: list[str] = []
        self.hours = {(player_id, appid): hours for player_id, appid, hours, _ in self.plays}
        self.hours_2w = {(player_id, appid): hours_2w for player_id, appid, _, hours_2w in self.plays}
        self.players_by_game: dict[str, set[str]] = defaultdict(set)
        self.games_by_player: dict[str, set[str]] = defaultdict(set)
        for player_id, appid, _, _ in self.plays:
            self.players_by_game[appid].add(player_id)
            self.games_by_player[player_id].add(appid)

    def close(self) -> None:
        pass

    def answer(self, rows: list[Any]) -> list[Any]:
        if self.random is not None:
            rows = list(rows)
            self.random.shuffle(rows)
        return rows

    # ---- graph helpers -------------------------------------------------------------------------

    def tag_edges(self, appid: str) -> list[tuple[str, str, str, int | None]]:
        """(tag_id, kind, name, rank) for every HAS_TAG edge of the game."""
        return [(tag_id(kind, name), kind, name, rank) for kind, name, rank in self.games[appid]["tags"]]

    def studio_edges(self, appid: str) -> list[tuple[str, str, str]]:
        """(studio_id, name, role) for every DEVELOPED_BY / PUBLISHED_BY edge of the game."""
        return [(studio_id(name), name, role) for name, role in self.games[appid]["studios"]]

    def game_row(self, appid: str) -> dict[str, Any]:
        game = self.games[appid]
        reviews = [review for review in REVIEWS if review[2] == appid]
        positive = sum(1 for review in reviews if review[4])
        return {
            "appid": appid,
            "name": game["name"],
            "name_ja": None,
            "description": f"{game['name']} description",
            "description_ja": None,
            "price": game["price"],
            "currency": "USD",
            "tags": [
                {"name": name, "name_ja": tag_ja(name), "rank": rank}
                for _, _, name, rank in self.tag_edges(appid)
                if rank is None or rank <= TAG_MAX_RANK
            ],
            "review_count": len(reviews),
            "recommend_rate": positive / len(reviews) if reviews else None,
            "played_count": len(self.players_by_game[appid]),
        }

    # ---- queries shared with the chat ----------------------------------------------------------

    def popular_games(self, limit: int) -> list[dict[str, Any]]:
        self.calls.append("popular_games")
        rows = [self.game_row(appid) for appid in self.games]
        rows.sort(key=lambda row: (-row["played_count"], -row["review_count"], row["appid"]))
        return self.answer(rows[:limit])

    def played_games(self, player_id: str, hide_appids: list[str]) -> list[dict[str, Any]]:
        self.calls.append("played_games")
        rows = [
            {
                "appid": appid,
                "name": self.games[appid]["name"],
                "hours": self.hours[(player_id, appid)],
                "hours_2w": self.hours_2w[(player_id, appid)],
            }
            for appid in self.games_by_player.get(player_id, set())
            if appid not in hide_appids
        ]
        rows.sort(key=lambda row: (-row["hours"], row["appid"]))
        return self.answer(rows)

    def tag_preferences(self, played: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
        self.calls.append("tag_preferences")
        total = len(self.games)
        preference: dict[str, float] = defaultdict(float)
        via_rows: dict[str, list[tuple[float, str, str]]] = defaultdict(list)
        names: dict[str, str] = {}
        for game in played:
            for current_tag_id, _, name, rank in self.tag_edges(game["appid"]):
                if rank is None or rank <= TAG_MAX_RANK:
                    preference[current_tag_id] += math.log(1 + game["hours"])
                    via_rows[current_tag_id].append((-game["hours"], game["appid"], self.games[game["appid"]]["name"]))
                    names[current_tag_id] = name
        rows = []
        for current_tag_id, pref in preference.items():
            document_frequency = sum(
                1 for appid in self.games for edge in self.tag_edges(appid) if edge[0] == current_tag_id
            )
            weight = pref * math.log(total / document_frequency)
            if weight > 0:
                rows.append(
                    {
                        "tag_id": current_tag_id,
                        "name": names[current_tag_id],
                        "name_ja": tag_ja(names[current_tag_id]),
                        "weight": weight,
                        "via": min(via_rows[current_tag_id])[2],
                    }
                )
        rows.sort(key=lambda row: (-row["weight"], row["tag_id"]))
        return self.answer(rows[:limit])

    # ---- v0 ranking queries --------------------------------------------------------------------

    def games_sharing_tags(self, prefs, played_appids, limit):
        self.calls.append("games_sharing_tags")
        hits_by_game: dict[str, list[tuple[float, str, dict[str, Any]]]] = defaultdict(list)
        for pref in prefs:
            for appid in self.games:
                if appid in played_appids:
                    continue
                for current_tag_id, _, name, rank in self.tag_edges(appid):
                    if current_tag_id == pref["tag_id"] and (rank is None or rank <= TAG_MAX_RANK):
                        hit = {"tag": name, "tag_ja": tag_ja(name), "via": pref["via"], "weight": pref["weight"]}
                        hits_by_game[appid].append((-pref["weight"], current_tag_id, hit))
        rows = []
        for appid, hits in hits_by_game.items():
            hits.sort(key=lambda item: (item[0], item[1]))
            rows.append(dict(self.game_row(appid), score=sum(-item[0] for item in hits), hits=[item[2] for item in hits]))
        rows.sort(key=lambda row: (-row["score"], row["appid"]))
        return self.answer(rows[:limit])

    def games_played_by_peers(self, player_id, seeds, played_appids, limit):
        self.calls.append("games_played_by_peers")
        seeds_by_peer: dict[str, list[tuple[float, str]]] = defaultdict(list)
        for seed in seeds:
            for peer in self.players_by_game[seed["appid"]]:
                if peer != player_id:
                    seeds_by_peer[peer].append((-seed["hours"], seed["appid"]))
        peers_by_game: dict[str, list[tuple[float, str]]] = defaultdict(list)
        for peer, peer_seeds in seeds_by_peer.items():
            if len(peer_seeds) < 2:
                continue
            via = min(peer_seeds)
            for appid in self.games_by_player[peer]:
                if appid not in played_appids:
                    peers_by_game[appid].append(via)
        rows = [
            dict(self.game_row(appid), peer_count=len(vias), via=self.games[min(vias)[1]]["name"])
            for appid, vias in peers_by_game.items()
        ]
        rows.sort(key=lambda row: (-row["peer_count"], row["appid"]))
        return self.answer(rows[:limit])

    def games_from_played_studios(self, played, played_appids, limit):
        self.calls.append("games_from_played_studios")
        weight_by_studio: dict[str, float] = defaultdict(float)
        seeds_by_studio: dict[str, list[tuple[float, str]]] = defaultdict(list)
        studio_names: dict[str, str] = {}
        for game in played:
            for current_studio_id, name, _ in self.studio_edges(game["appid"]):
                weight_by_studio[current_studio_id] += math.log(1 + game["hours"])
                seeds_by_studio[current_studio_id].append((-game["hours"], game["appid"]))
                studio_names[current_studio_id] = name
        links_by_game: dict[str, list[tuple[float, str]]] = defaultdict(list)
        for appid in self.games:
            if appid in played_appids:
                continue
            for current_studio_id, _, _ in self.studio_edges(appid):
                if current_studio_id in weight_by_studio:
                    links_by_game[appid].append((-weight_by_studio[current_studio_id], current_studio_id))
        rows = []
        for appid, links in links_by_game.items():
            best_studio = min(links)[1]
            via_appid = min(seeds_by_studio[best_studio])[1]
            rows.append(
                dict(
                    self.game_row(appid),
                    score=weight_by_studio[best_studio],
                    studio=studio_names[best_studio],
                    via=self.games[via_appid]["name"],
                )
            )
        rows.sort(key=lambda row: (-row["score"], row["appid"]))
        return self.answer(rows[:limit])

    # ---- static recommendation paths (M3), written from the formulas ------------------------------

    def others(self, player_id: str, appid: str) -> set[str]:
        return self.players_by_game[appid] - {player_id}

    def co_play_scores(self, player_id, seeds, played_appids, evidence_limit):
        self.calls.append("co_play_scores")
        rows = []
        for appid in self.games:
            if appid in played_appids:
                continue
            instances = []
            for seed in seeds:
                co_players = len(self.others(player_id, seed["appid"]) & self.others(player_id, appid))
                if not co_players:
                    continue
                seed_players = len(self.others(player_id, seed["appid"]))
                game_players = len(self.others(player_id, appid))
                instances.append(
                    {
                        "appid": seed["appid"],
                        "name": self.games[seed["appid"]]["name"],
                        "weight": seed["weight"],
                        "co_players": co_players,
                        "seed_players": seed_players,
                        "term": seed["weight"] * co_players / math.sqrt(seed_players * game_players),
                    }
                )
            if instances:
                instances.sort(key=lambda item: (-item["term"], item["appid"]))
                rows.append(
                    {
                        "appid": appid,
                        "score": sum(item["term"] for item in instances),
                        "game_players": len(self.others(player_id, appid)),
                        "seed_count": len(instances),
                        "seeds": instances[:evidence_limit],
                    }
                )
        rows.sort(key=lambda row: (-row["score"], row["appid"]))
        return self.answer(rows)

    def concepts(self, appid: str) -> set[str]:
        """Merged tag names of a game: no Feature tags, only edges used for matching."""
        return {
            current_tag_id.split(":", 1)[1]
            for current_tag_id, kind, _, rank in self.tag_edges(appid)
            if kind != "feature" and (rank is None or rank <= TAG_MAX_RANK)
        }

    def concept_label(self, concept: str) -> tuple[str, str | None]:
        """Name of the concept's tag with the smallest tag_id (the genre when there is one)."""
        candidates = sorted(
            (current_tag_id, name)
            for appid in self.games
            for current_tag_id, kind, name, _ in self.tag_edges(appid)
            if kind != "feature" and current_tag_id.split(":", 1)[1] == concept
        )
        name = candidates[0][1]
        return name, tag_ja(name)

    def tag_concept_scores(self, seeds, played_appids, concept_limit, evidence_limit):
        self.calls.append("tag_concept_scores")
        total = len(self.games)
        concepts_by_game = {appid: self.concepts(appid) for appid in self.games}
        preference: dict[str, float] = defaultdict(float)
        via: dict[str, tuple[float, str]] = {}
        for seed in seeds:
            for concept in concepts_by_game[seed["appid"]]:
                preference[concept] += seed["weight"]
                via[concept] = min(via.get(concept, (math.inf, "")), (-seed["weight"], seed["appid"]))
        weights = {}
        for concept, pref in preference.items():
            document_frequency = sum(1 for appid in self.games if concept in concepts_by_game[appid])
            weight = pref * math.log(total / document_frequency)
            if weight > 0:
                weights[concept] = weight
        kept = sorted(weights, key=lambda concept: (-weights[concept], concept))[:concept_limit]
        rows = []
        for appid in self.games:
            if appid in played_appids:
                continue
            own = concepts_by_game[appid]
            instances = []
            for concept in kept:
                if concept in own:
                    name, name_ja = self.concept_label(concept)
                    seed_appid = via[concept][1]
                    instances.append(
                        {
                            "concept": concept,
                            "name": name,
                            "name_ja": name_ja,
                            "weight": weights[concept],
                            "seed_appid": seed_appid,
                            "seed_name": self.games[seed_appid]["name"],
                            "term": weights[concept] / math.sqrt(len(own)),
                        }
                    )
            if instances:
                instances.sort(key=lambda item: (-item["term"], item["concept"]))
                rows.append(
                    {
                        "appid": appid,
                        "score": sum(item["term"] for item in instances),
                        "concept_count": len(own),
                        "match_count": len(instances),
                        "matches": instances[:evidence_limit],
                    }
                )
        rows.sort(key=lambda row: (-row["score"], row["appid"]))
        return self.answer(rows)

    def studio_roles(self, appid: str) -> dict[str, str]:
        """studio_id -> "developer" if the game has DEVELOPED_BY to it, else "publisher"."""
        roles: dict[str, str] = {}
        for current_studio_id, _, role in self.studio_edges(appid):
            if roles.get(current_studio_id) != "developer":
                roles[current_studio_id] = role
        return roles

    def studio_scores(self, seeds, played_appids, developer_weight, publisher_weight, evidence_limit):
        self.calls.append("studio_scores")
        names = {sid: name for appid in self.games for sid, name, _ in self.studio_edges(appid)}
        titles: dict[str, set[str]] = defaultdict(set)
        for appid in self.games:
            for current_studio_id in self.studio_roles(appid):
                titles[current_studio_id].add(appid)
        rows = []
        for appid in self.games:
            if appid in played_appids:
                continue
            game_roles = self.studio_roles(appid)
            links = []
            for seed in seeds:
                seed_roles = self.studio_roles(seed["appid"])
                options = []
                for current_studio_id in set(seed_roles) & set(game_roles):
                    both_developed = seed_roles[current_studio_id] == game_roles[current_studio_id] == "developer"
                    role_weight = developer_weight if both_developed else publisher_weight
                    term = seed["weight"] * role_weight / math.log(2 + len(titles[current_studio_id]))
                    options.append((-term, current_studio_id))
                if options:
                    negative_term, best = min(options)
                    links.append(
                        {
                            "studio_id": best,
                            "studio": names[best],
                            "seed_role": seed_roles[best],
                            "game_role": game_roles[best],
                            "titles": len(titles[best]),
                            "term": -negative_term,
                            "seed_appid": seed["appid"],
                            "seed_name": self.games[seed["appid"]]["name"],
                            "weight": seed["weight"],
                        }
                    )
            if links:
                links.sort(key=lambda link: (-link["term"], link["seed_appid"]))
                rows.append(
                    {
                        "appid": appid,
                        "score": sum(link["term"] for link in links),
                        "seed_count": len(links),
                        "links": links[:evidence_limit],
                    }
                )
        rows.sort(key=lambda row: (-row["score"], row["appid"]))
        return self.answer(rows)

    def game_popularity(self, player_id, played_appids):
        self.calls.append("game_popularity")
        rows = [
            {"appid": appid, "players": len(self.others(player_id, appid))}
            for appid in self.games
            if appid not in played_appids
        ]
        rows.sort(key=lambda row: (-row["players"], row["appid"]))
        return self.answer(rows)

    def games_by_appids(self, appids):
        self.calls.append("games_by_appids")
        return [self.game_row(appid) for appid in appids if appid in self.games]

    # ---- chat-only queries ---------------------------------------------------------------------

    def top_reviews(self, appid: str, limit: int) -> list[dict[str, Any]]:
        self.calls.append("top_reviews")
        rows = [
            {
                "text": text,
                "recommended": recommended,
                "votes_up": votes_up,
                "created_at": created_at,
                "hours": self.hours.get((player_id, appid)),
                "review_id": review_id,
            }
            for review_id, player_id, review_appid, text, recommended, votes_up, created_at, _ in REVIEWS
            if review_appid == appid and len(text) > 10
        ]
        rows.sort(key=lambda row: (-row["votes_up"], row["review_id"]))
        for row in rows:
            row.pop("review_id")
        return rows[:limit]

    def sample_players(self, limit: int) -> list[dict[str, Any]]:
        self.calls.append("sample_players")
        rows = [
            {"player_id": player_id, "played_count": len(appids)}
            for player_id, appids in sorted(self.games_by_player.items())
            if len(appids) >= 3
        ]
        return rows[:limit]

    def player_reviews(self, player_id: str, hide_appids: list[str], limit: int) -> list[dict[str, Any]]:
        self.calls.append("player_reviews")
        rows = [
            {"name": self.games[appid]["name"], "recommended": recommended, "text": text, "votes_up": votes_up}
            for _, author, appid, text, recommended, votes_up, _, _ in REVIEWS
            if author == player_id and appid not in hide_appids
        ]
        rows.sort(key=lambda row: -row["votes_up"])
        return [{key: row[key] for key in ("name", "recommended", "text")} for row in rows[:limit]]

    def player_aspects(self, player_id: str, hide_appids: list[str], limit: int) -> list[dict[str, Any]]:
        self.calls.append("player_aspects")
        counts: dict[str, int] = defaultdict(int)
        for _, author, appid, _, _, _, _, aspects in REVIEWS:
            if author == player_id and appid not in hide_appids:
                for aspect in aspects:
                    counts[aspect] += 1
        rows = [{"aspect": aspect, "count": count} for aspect, count in counts.items()]
        rows.sort(key=lambda row: (-row["count"], row["aspect"]))
        return rows[:limit]

    def top_tags(self, limit: int) -> list[str]:
        self.calls.append("top_tags")
        counts: dict[str, int] = defaultdict(int)
        for appid in self.games:
            for _, _, name, rank in self.tag_edges(appid):
                if rank is None or rank <= TAG_MAX_RANK:
                    counts[name] += 1
        return [name for name, _ in sorted(counts.items(), key=lambda item: (-item[1], item[0]))][:limit]

    def aspect_names(self) -> list[str]:
        self.calls.append("aspect_names")
        return sorted(SEED_ASPECTS)

    def search_games(self, tags, aspects, keywords, max_price, limit):
        self.calls.append("search_games")
        rows = []
        for appid, game in self.games.items():
            if max_price is not None and game["price"] is not None and game["price"] > max_price:
                continue
            matched_tags = [
                name
                for _, _, name, rank in self.tag_edges(appid)
                if name in tags and (rank is None or rank <= TAG_MAX_RANK)
            ]
            game_reviews = [review for review in REVIEWS if review[2] == appid]
            matched_aspects = [aspect for review in game_reviews for aspect in review[7] if aspect in aspects]
            keyword_hits = sum(
                1 for review in game_reviews if any(keyword.lower() in review[3].lower() for keyword in keywords)
            )
            score = len(matched_tags) * 3.0 + math.log(1 + len(matched_aspects)) + math.log(1 + keyword_hits)
            if score > 0:
                rows.append(
                    dict(
                        self.game_row(appid),
                        score=score,
                        matched_tags=matched_tags,
                        matched_aspects=matched_aspects,
                        keyword_hits=keyword_hits,
                    )
                )
        rows.sort(key=lambda row: (-row["score"], row["appid"]))
        return rows[:limit]

    def all_play_records(self) -> list[tuple[str, str, float]]:
        return [(player_id, appid, hours) for player_id, appid, hours, _ in self.plays]

    def tag_ids_by_game(self) -> dict[str, set[str]]:
        return {
            appid: {edge[0] for edge in self.tag_edges(appid) if edge[3] is None or edge[3] <= TAG_MAX_RANK}
            for appid in self.games
        }
