from utils.graph_reader import GraphReader

TOP_PLAYED_GAMES = 10
TOP_TAGS = 10
TOP_REVIEWS = 3
TOP_ASPECTS = 8
REVIEW_EXCERPT_CHARS = 200

RECOMMENDED_LABELS = {True: "おすすめする", False: "おすすめしない", None: "不明"}


def build_player_profile(reader: GraphReader, player_id: str, hidden_appids: set[str]) -> str:
    hidden = sorted(hidden_appids)
    played_games = reader.played_games(player_id, hidden)
    tag_preferences = reader.tag_preferences(played_games, TOP_TAGS) if played_games else []
    reviews = reader.player_reviews(player_id, hidden, TOP_REVIEWS)
    aspect_counts = reader.player_aspects(player_id, hidden, TOP_ASPECTS)

    lines = ["## プレイ時間の長いゲーム"]
    lines += [f"- {game['name']}（{game['hours']:.1f} 時間）" for game in played_games[:TOP_PLAYED_GAMES]]
    recent_games = [game for game in played_games if game["hours_2w"] > 0]
    if recent_games:
        lines.append("## 直近2週間に遊んだゲーム")
        lines += [f"- {game['name']}（{game['hours_2w']:.1f} 時間）" for game in recent_games[:TOP_PLAYED_GAMES]]
    if tag_preferences:
        lines.append("## 好むタグ")
        lines.append(", ".join(preference["name"] for preference in tag_preferences))
    if reviews:
        lines.append("## 書いたレビュー")
        for review in reviews:
            verdict = RECOMMENDED_LABELS[review["recommended"]]
            lines.append(f"- {review['name']}（{verdict}）: {review['text'][:REVIEW_EXCERPT_CHARS]}")
    if aspect_counts:
        lines.append("## レビューで言及した観点")
        lines.append(", ".join(f"{item['aspect']}（{item['count']}）" for item in aspect_counts))
    return "\n".join(lines)
