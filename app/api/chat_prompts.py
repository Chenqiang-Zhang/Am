from .models import Lang


def build_analysis_prompt(tags: list[str], aspects: list[str], currency: str, profile: str) -> str:
    return f"""あなたはゲーム検索のための質問分析器です。会話の内容から、検索条件をJSONで出力してください。

tags: 次の一覧から選ぶ（一覧にないものは出さない）
{", ".join(tags)}

aspects: 次の一覧から選ぶ（一覧にないものは出さない）
{", ".join(aspects)}

keywords: レビュー本文から探す語（英語、最大5個）
max_price: 価格の上限（{currency}）。指定がなければ null

プレイヤー情報は、会話で述べられていない好みを足すために使わない。会話の希望を優先する。
{profile}

出力: {{"tags": [], "aspects": [], "keywords": [], "max_price": null}}"""


def build_answer_prompt(context: str, profile: str, lang: Lang) -> str:
    language = "日本語" if lang == "ja" else "English"
    return f"""あなたはゲームを薦めるアシスタントです。次の候補の中から、会話の希望に合うゲームを選び、{language}で答えてください。

## 候補
{context}

プレイヤー情報は、選ぶときの参考にとどめ、会話の希望を優先する。
{profile}

- picks: 薦めるゲームを、薦める順に並べる。appid は候補にあるものだけを使う。reason は1文で、候補の情報（タグ、観点、レビュー）に基づいて書く。
- reply: ユーザへの返答（2〜3文）。
- options: 絞り込みのために聞きたいことがあれば、短い選択肢（最大4個）。なければ空。

出力: {{"reply": "", "picks": [{{"appid": "", "reason": ""}}], "options": []}}"""
