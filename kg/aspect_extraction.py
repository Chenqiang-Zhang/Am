import json
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from utils.llm_client import LlmClient

from .files import graph_data_dir, read_jsonl, write_jsonl
from .ids import to_snake_case

SEED_ASPECTS = {
    "story": "ストーリー",
    "characters": "キャラクター",
    "gameplay": "ゲームプレイ",
    "combat": "戦闘",
    "level_design": "レベルデザイン",
    "progression": "成長・進行",
    "content_volume": "コンテンツ量",
    "replayability": "リプレイ性",
    "multiplayer": "マルチプレイ",
    "humor": "ユーモア",
    "controls": "操作性",
    "difficulty": "難易度",
    "ui": "UI",
    "customization": "カスタマイズ",
    "graphics": "グラフィック",
    "sound_music": "サウンド・音楽",
    "atmosphere": "雰囲気",
    "performance": "パフォーマンス",
    "stability": "安定性",
    "value_for_money": "コストパフォーマンス",
}
ASPECT_DEFINITIONS = {
    "story": "物語、シナリオ、ストーリー展開",
    "characters": "キャラクター、NPC、人物の魅力",
    "gameplay": "遊びの仕組み、ゲームシステム、ゲームのバランス",
    "combat": "戦闘、銃や武器の扱い、戦闘の手応え",
    "level_design": "マップ、地形、探索、世界の作り",
    "progression": "成長、レベル上げ、アンロック、ドロップ、報酬、作業感",
    "content_volume": "遊べる量、ゲームモード、DLC やコンテンツの多さ・少なさ",
    "replayability": "繰り返し遊ぶ価値、長く遊べるか、飽きるか",
    "multiplayer": "他のプレイヤーとの関わり（協力、対戦、チームメイト、チート、フレンド、コミュニティ）",
    "humor": "ゲーム自体の笑い、ユーモア",
    "controls": "操作性、入力デバイス（キーボード、コントローラー）",
    "difficulty": "難しさ、歯ごたえ、挑戦",
    "ui": "画面表示、メニュー、操作画面",
    "customization": "見た目や装備の変更、スキン、改造",
    "graphics": "映像、グラフィック、景色の美しさ",
    "sound_music": "音楽、効果音、ボイス",
    "atmosphere": "雰囲気、世界観の空気感、リラックスできるか",
    "performance": "動作の軽さ、フレームレート、動作環境",
    "stability": "バグ、クラッシュ、起動できない、接続の不具合",
    "value_for_money": "価格、課金、無料、DLC の値付け、買う価値",
}
REVIEWS_PER_REQUEST = 1
MIN_REVIEW_CHARS = 30
MAX_REVIEWS_PER_GAME = 10
MAX_REVIEW_CHARS = 1000
FEW_SHOT_PATH = Path(__file__).parent / "aspect_few_shot.json"

SYSTEM_PROMPT = """あなたはゲームのレビューから、言及されている評価の観点を選ぶ分析器です。
観点の一覧（観点名: 意味）:
{aspects}

各レビューについて、本文が明示的に触れている観点だけを一覧から選んでください。好意的か否定的かは問いません。
「面白い」「最高」のような総評だけでは、観点を付けないでください。本文にない観点を推測しないでください。
一覧にない観点は、付けないでください。
言及がなければ空にしてください。
各レビューは id と text を持ちます。結果の id には、渡された id をそのまま書いてください。

出力: {{"results": [{{"id": "", "aspects": []}}]}}"""

VERIFY_PROMPT = """あなたはゲームのレビューを読む検証器です。
ユーザが渡すレビュー本文が、次の観点に、明示的に触れているかを答えてください。
観点: {aspect}（意味: {definition}）

触れている: 本文が、その観点について、具体的に述べている（好意的か否定的かは問いません）。
触れていない: 「面白い」「最高」のような総評だけ、本文にない内容の推測、観点と関係のない話。

出力: {{"mentioned": true}} または {{"mentioned": false}}"""


def build_aspects_text(aspects: dict[str, str]) -> str:
    lines = []
    for aspect in aspects:
        definition = ASPECT_DEFINITIONS.get(aspect)
        if definition:
            lines.append(f"- {aspect}: {definition}")
        else:
            lines.append(f"- {aspect}")
    return "\n".join(lines)


def request_aspects(llm_client: LlmClient, aspects_text: str, batch: list[dict]) -> dict[str, list[str]]:
    request_reviews = []
    review_id_by_number = {}
    for number, review in enumerate(batch, start=1):
        review_id_by_number[str(number)] = review["review_id"]
        request_reviews.append({"id": str(number), "text": review["text"][:MAX_REVIEW_CHARS]})
    messages = [{"role": "system", "content": SYSTEM_PROMPT.format(aspects=aspects_text)}]
    examples = json.loads(FEW_SHOT_PATH.read_text(encoding="utf-8"))["examples"]
    for example in examples:
        example_reviews = [{"id": "1", "text": example["text"]}]
        example_answer = {"results": [{"id": "1", "aspects": example["aspects"]}]}
        messages.append({"role": "user", "content": json.dumps(example_reviews, ensure_ascii=False)})
        messages.append({"role": "assistant", "content": json.dumps(example_answer, ensure_ascii=False)})
    messages.append({"role": "user", "content": json.dumps(request_reviews, ensure_ascii=False)})
    response = llm_client.complete_json(messages)

    names_by_review_id = {review["review_id"]: [] for review in batch}
    for result in response.get("results", []):
        review_id = review_id_by_number.get(str(result.get("id")))
        if review_id is None:
            continue
        aspects = result.get("aspects", [])
        if not isinstance(aspects, list):
            aspects = []
        names = [to_snake_case(str(aspect)) for aspect in aspects]
        names_by_review_id[review_id] = [name for name in dict.fromkeys(names) if name in SEED_ASPECTS]
    return names_by_review_id


def verify_aspect(llm_client: LlmClient, review_text: str, aspect: str) -> bool:
    definition = ASPECT_DEFINITIONS.get(aspect, "")
    response = llm_client.complete_json(
        [
            {"role": "system", "content": VERIFY_PROMPT.format(aspect=aspect, definition=definition)},
            {"role": "user", "content": review_text[:MAX_REVIEW_CHARS]},
        ]
    )
    mentioned = response.get("mentioned")
    if isinstance(mentioned, str):
        mentioned = mentioned.strip().lower() == "true"
    return bool(mentioned)


def extract_aspects(graph_name: str, llm_client: LlmClient | None = None, workers: int = 1) -> None:
    directory = graph_data_dir(graph_name)
    llm_client = llm_client or LlmClient("build")
    mentions_path = directory / "mentions.jsonl"
    aspects_text = build_aspects_text(SEED_ASPECTS)
    done_review_ids = {mention["review_id"] for mention in read_jsonl(mentions_path)}

    reviews_by_appid = defaultdict(list)
    seen_review_ids = set()
    for review in read_jsonl(directory / "reviews.jsonl"):
        if len(review["text"]) < MIN_REVIEW_CHARS or review["review_id"] in seen_review_ids:
            continue
        seen_review_ids.add(review["review_id"])
        reviews_by_appid[review["appid"]].append(review)

    pending_reviews = []
    for reviews in reviews_by_appid.values():
        reviews.sort(key=lambda review: (-(review["votes_up"] or 0), -len(review["text"]), review["review_id"]))
        for review in reviews[:MAX_REVIEWS_PER_GAME]:
            if review["review_id"] not in done_review_ids:
                pending_reviews.append(review)
    batches = [
        pending_reviews[start : start + REVIEWS_PER_REQUEST]
        for start in range(0, len(pending_reviews), REVIEWS_PER_REQUEST)
    ]
    for wave_start in range(0, len(batches), workers):
        wave = batches[wave_start : wave_start + workers]
        with ThreadPoolExecutor(max_workers=workers) as executor:
            responses = list(executor.map(lambda batch: request_aspects(llm_client, aspects_text, batch), wave))

        mentions = []
        for names_by_review_id in responses:
            for review_id, names in names_by_review_id.items():
                mentions.append({"review_id": review_id, "aspects": names})
        write_jsonl(mentions_path, mentions, append=True)
        processed = sum(len(batch) for batch in batches[: wave_start + len(wave)])
        print(f"{graph_name}: aspects {processed}/{len(pending_reviews)}")

