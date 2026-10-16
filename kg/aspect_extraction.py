import json
from concurrent.futures import ThreadPoolExecutor

from utils.llm_client import LlmClient

from .files import graph_data_dir, read_jsonl, write_jsonl
from .ids import to_snake_case

SEED_ASPECTS = [
    "story", "characters", "gameplay", "combat", "level_design", "progression", "content_volume",
    "replayability", "multiplayer", "humor", "controls", "difficulty", "ui", "customization",
    "graphics", "sound_music", "atmosphere", "performance", "stability", "value_for_money",
]
REVIEWS_PER_REQUEST = 10
MIN_REVIEW_CHARS = 30
MAX_REVIEW_CHARS = 1000

SYSTEM_PROMPT = """あなたはゲームのレビューから、言及されている評価の観点を選ぶ分析器です。
観点の一覧:
{aspects}

各レビューについて、言及している観点を一覧から選んでください。好意的か否定的かは問いません。
一覧のどれにも当てはまらない観点が言及されている場合は、英語・小文字・アンダースコアの新しい観点名を追加してください。
言及がなければ空にしてください。

出力: {{"results": [{{"id": "", "aspects": []}}]}}"""


def request_aspects(llm_client: LlmClient, aspects_text: str, batch: list[dict]) -> dict:
    request_reviews = [{"id": review["review_id"], "text": review["text"][:MAX_REVIEW_CHARS]} for review in batch]
    return llm_client.complete_json(
        [
            {"role": "system", "content": SYSTEM_PROMPT.format(aspects=aspects_text)},
            {"role": "user", "content": json.dumps(request_reviews, ensure_ascii=False)},
        ]
    )


def extract_aspects(graph_name: str, llm_client: LlmClient | None = None, workers: int = 1) -> None:
    directory = graph_data_dir(graph_name)
    llm_client = llm_client or LlmClient("build")
    aspects_path = directory / "aspects.json"
    mentions_path = directory / "mentions.jsonl"
    aspects = json.loads(aspects_path.read_text()) if aspects_path.exists() else list(SEED_ASPECTS)
    done_review_ids = {mention["review_id"] for mention in read_jsonl(mentions_path)}

    pending_reviews = [
        review
        for review in read_jsonl(directory / "reviews.jsonl")
        if review["review_id"] not in done_review_ids and len(review["text"]) >= MIN_REVIEW_CHARS
    ]
    batches = [
        pending_reviews[start : start + REVIEWS_PER_REQUEST]
        for start in range(0, len(pending_reviews), REVIEWS_PER_REQUEST)
    ]
    for wave_start in range(0, len(batches), workers):
        wave = batches[wave_start : wave_start + workers]
        aspects_text = ", ".join(aspects)
        with ThreadPoolExecutor(max_workers=workers) as executor:
            responses = list(executor.map(lambda batch: request_aspects(llm_client, aspects_text, batch), wave))

        mentions = []
        for batch, response in zip(wave, responses):
            batch_review_ids = {review["review_id"] for review in batch}
            for result in response.get("results", []):
                review_id = str(result["id"])
                if review_id not in batch_review_ids:
                    continue
                names = [to_snake_case(str(aspect)) for aspect in result.get("aspects", [])]
                names = [name for name in dict.fromkeys(names) if name]
                aspects += [name for name in names if name not in aspects]
                mentions.append({"review_id": review_id, "aspects": names})
        write_jsonl(mentions_path, mentions, append=True)
        aspects_path.write_text(json.dumps(aspects, ensure_ascii=False))
        processed = sum(len(batch) for batch in batches[: wave_start + len(wave)])
        print(f"{graph_name}: aspects {processed}/{len(pending_reviews)}")
    aspects_path.write_text(json.dumps(aspects, ensure_ascii=False))
