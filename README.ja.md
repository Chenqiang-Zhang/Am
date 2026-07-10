# Amazon Reviews'23 ナレッジグラフ推薦システム

[English](README.md) | **日本語** | [中文](README.zh-CN.md)

Amazon Reviews'23 データセットを用いた実験的な商品推薦システム。ジャンル／カテゴリは `config.yaml` で切り替え可能で（現在は `Video_Games` で、以下のデフォルト値・例は特に断りなくこのジャンルを前提とする）、スキーマ・パイプラインスクリプト・Cypher クエリはすべてジャンル非依存に設計されている。本システムはレビューデータと商品メタデータを Neo4j ナレッジグラフへと変換し、LLM がグラフに対して直接 Cypher クエリを書いて実行する REST API を公開する — そのため、推薦理由はすべて実際に検証可能なグラフパスであり、ブラックボックスにはならない。

## アーキテクチャ

```
生データ（Amazon Reviews'23）
    ↓ select_kcore.py（k-core によるユーザ・商品選定を決定 — データ規模）
    ↓ build_base_graph.py
ベースグラフ CSV（Product/User/Review/Category/Brand）
    ↓ extract_product_attributes.py（ルールベースの `details` 抽出 + LLM によるテキスト抽出を統合）
    ↓ extract_review_mentions.py（LLM、レビュー本文から抽出）
    ↓ canonicalize_attributes.py（任意 — LLM による attr_type/value の同義語正規化）
    ↓ build_attribute_graph.py
属性ノード/エッジ CSV（ジャンル非依存の attr_type）
    ↓ import_kg_to_neo4j.py（上記すべてを一括インポート）
Neo4j ナレッジグラフ
    ↓ backfill_display_fields.py --images --titles-ja（任意 — Product.image_url / Product.title_ja）
    ↓
REST API（FastAPI）
    ├── Text2Cypher 検索        （LLM がリクエストごとに Cypher クエリを書いて実行。グラフスキーマと
    │                            attr_type の語彙は稼働中のグラフから読み取るため、実際にロードされて
    │                            いるジャンル／カタログにプロンプトが適応する — カテゴリのハードコード
    │                            なし。生成・実行に失敗した場合、または結果が正当に0件だった場合は
    │                            人気度クエリにフォールバックする）
    ├── パーソナライゼーション   （ユーザの評価・属性履歴 + 過去の成功クエリを few-shot として利用。
    │                            $uid は user_id に実際の RATED／属性履歴がある場合にのみバインドされる）
    ├── ホーム推薦               （行動ベースでクエリテキストなし。履歴のないユーザに対しては LLM を
    │                            完全にスキップし — 人気度クエリへ即座にフォールバックするためレイテンシ
    │                            増加なし — さらにパーソナライズされた各ユーザの生成クエリをメモリに
    │                            キャッシュすることで、タブが非表示／クローズされた際に発火するバック
    │                            グラウンドの「warm」呼び出しにより、次回のページ表示を即時にできる）
    ├── 対話チャット             （LLM が同じ稼働中の attr_type 語彙に基づき、何を尋ねるか・いつ検索する
    │                            かを自分で決定 — ジャンル非依存。Python 側は質問数のハードキャップと、
    │                            LLM 呼び出し自体が失敗した場合のフォールバックのみを強制する）
    └── レビュー参照・閲覧ログ記録（すべて Neo4j に書き込み）
```

## グラフスキーマ

正式なスキーマ定義は [`Graph_rule.md`](Graph_rule.md)（リポジトリルートの [`../Graph_rule.md`](../Graph_rule.md) と内容を同期している）。概要は以下の通り。

**ノード**

| ラベル | 主なプロパティ |
|---|---|
| `User` | `user_id` |
| `Product` | `product_id`, `title`, `title_ja`, `price`, `avg_rating`, `rating_count`, `description`, `image_url` |
| `Review` | `review_id`, `title`, `text`, `rating`, `timestamp`, `helpful_vote`, `verified` |
| `Category` | `category_id`, `name`, `level` |
| `Brand` | `brand_id`, `name` |
| `Attribute` | `attribute_id`, `attr_type`, `value`（LLM 抽出、ジャンル非依存） |
| `SearchLog` | `log_id`, `query`, `cypher`, `explanation`, `result_product_ids`, `result_count`, `timestamp`（パーソナライゼーション用 few-shot） |

**リレーションシップ**

| リレーションシップ | 方向 | プロパティ |
|---|---|---|
| `WROTE` | User → Review | — |
| `ABOUT` | Review → Product | — |
| `RATED` | User → Product | `rating`, `timestamp` |
| `VIEWED` | User → Product | `timestamp`, `search_id` |
| `SEARCHED` | User → SearchLog | — |
| `BELONGS_TO` | Product → Category | — |
| `SUBCATEGORY_OF` | Category → Category | — |
| `MADE_BY` | Product → Brand | — |
| `HAS_ATTRIBUTE` | Product → Attribute | `confidence`, `evidence`, `source`, `model` |
| `MENTIONS` | Review → Attribute | `sentiment`, `confidence` |

以前のバージョンにあった `Store`/`Feature` ノードおよび `HAS_FEATURE`/`REVIEWS` リレーションシップは廃止された — feature テキストは `Product.description` に統合され、`Store` は `Brand` に統合された。

## リポジトリ構成

```text
.
├── README.md
├── Graph_rule.md          # スキーマ定義書の完全なコピー。../Graph_rule.md と手動で同期
├── config.yaml            # ジャンル／データ規模／LLM プロバイダ／API 設定
├── requirements.txt
├── data/                  # 生データ — ローカルのみ、コミットしない
├── kg_output/             # 生成された CSV — ローカルのみ、コミットしない
├── docs/                  # ローカルドキュメント — コミットしない
├── kg_build/              # KG構築パイプライン — データ生成（実行: python kg_build/<name>.py）
│   ├── select_kcore.py            # 1. k-core によるユーザ・商品選定を決定（データ規模）
│   ├── build_base_graph.py        # 2. ベースグラフ CSV を構築（Product/User/Review/Category/Brand）
│   ├── extract_product_attributes.py  # 3a. 属性抽出: ゼロコストのルールベース（メタデータ `details`、ジャンル非依存）+ LLM（title/features/description）を統合
│   ├── extract_review_mentions.py     # 3b. レビュー本文からの LLM による属性言及抽出
│   ├── canonicalize_attributes.py     # 4.（任意）LLM による attr_type/value の同義語正規化
│   ├── build_attribute_graph.py       # 5. 上記の抽出結果を統合 → 属性ノード/エッジ CSV
│   ├── import_kg_to_neo4j.py          # 6. Bolt 経由ですべてをインポート（ローカル Neo4j または Aura）
│   ├── backfill_display_fields.py     # 7.（任意）既存ノードに Product.image_url / Product.title_ja / Review.title_ja+text_ja を追加
│   ├── wipe_neo4j.py                  #（ユーティリティ）設定された Neo4j インスタンスの全データを削除
│   └── utils/                         # kg_build/ 内でのみ使う共有ヘルパーモジュール — 直接実行はしない
│       ├── llm_client.py              #   LLM クライアントビルダー（gemini/groq/deepseek/openai/ollama）
│       ├── llm_json.py                #   Chat/responses の JSON 呼び出し + バッチ＋フォールバックのヘルパー
│       ├── neo4j_io.py                #   .env 読み込み + Neo4j 接続の解決
│       ├── csv_io.py                  #   JSONL/CSV の読み書きヘルパー
│       └── text_utils.py              #   テキストクレンジング／attr_type 正規化／ID ハッシュ化のヘルパー
├── eval/                  # 評価 — 稼働中のグラフ + app/api/ を参照。kg_build/ とは独立
│   └── eval_offline.py            # オフライン leave-one-out 評価（HR@K/NDCG@K を Item-KNN ベースラインと比較）
├── app/                   # 稼働中のアプリケーション（バックエンド＋フロントエンド）
│   ├── api/                       # 推薦 REST API
│   │   ├── main.py                # FastAPI アプリ／ルーティング
│   │   ├── recommender.py         # Text2Cypher 生成、チャット、パーソナライゼーション、レビュー
│   │   └── models.py              # Pydantic リクエスト/レスポンスモデル
│   └── web/                       # React + TypeScript による対話型 UI
```

## クイックスタート

### 1. 依存関係のインストール

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. 環境設定

```bash
cp .env.example .env
```

`.env` を以下の内容で編集する:
- `NEO4J_URI` / `NEO4J_PASSWORD`（Neo4j Aura の接続情報）
- `config.yaml`（`llm.provider`）で選択した LLM プロバイダに対応する API キー: `GEMINI_API_KEY`、`GROQ_API_KEY`、`DEEPSEEK_API_KEY`、または `OPENAI_API_KEY`。Gemini と Groq はどちらも無料枠がある。

`config.yaml` は LLM プロバイダ／モデル、データパス、Text2Cypher のリトライ設定を制御する — 詳細は同ファイル内のコメントを参照。商品／ユーザの選定は config の値ではなく、k-core のサイズ（`select_kcore.py` の `--k`、後述のステップ4を参照）で別途制御される。

### 3. Neo4j を起動する

**オプション A — Docker（ローカル開発）:**

```bash
docker run -d --name neo4j-am \
  -p 7474:7474 -p 7687:7687 \
  -e NEO4J_AUTH=neo4j/password123 \
  neo4j:latest
```

続いて `.env` に以下を設定する:
```
NEO4J_URI=bolt://localhost:7687
NEO4J_USERNAME=neo4j
NEO4J_PASSWORD=password123
NEO4J_DATABASE=neo4j
```

**オプション B — Neo4j Aura（クラウド）:** `.env` に `NEO4J_URI=neo4j+s://...` を設定する。無料枠の Aura インスタンスは非アクティブ状態が続くと自動的に一時停止するため、API やパイプラインスクリプトを実行する前に [Aura console](https://console.neo4j.io) から再開しておく。

### 4. ナレッジグラフの構築とインポート

Amazon Reviews'23 のデータファイルをローカルに配置する（パスは `config.yaml` の `data` セクションで指定。現在は `Video_Games` カテゴリ）:
```text
data/Video_Games.jsonl.gz
data/meta_Video_Games.jsonl.gz
```

データセットの生ファイルホストから直接ダウンロードする（`Video_Games` の場合、合計で約1GB。別のジャンルを使う場合は両方の URL のカテゴリ名を差し替える）:
```bash
wget -P data https://mcauleylab.ucsd.edu/public_datasets/data/amazon_2023/raw/review_categories/Video_Games.jsonl.gz
wget -P data https://mcauleylab.ucsd.edu/public_datasets/data/amazon_2023/raw/meta_categories/meta_Video_Games.jsonl.gz
```

パイプラインを順番に実行する（すべて `kg_build/` 配下。`import_kg_to_neo4j.py` の docstring と対応）:

```bash
# 1. どのユーザ／商品を含めるかを決定する: すべてのユーザ・商品が少なくとも k 件の
#    相互作用を持つ二部グラフの k-core（デフォルトは k=14 — Video_Games 向けに調整済み。
#    他の k 値で何が得られるかは後述の「データ規模」を参照し、ジャンルを変更する場合は
#    再度スイープすること）。デフォルトでは、price/avg_rating/rating_count/description/
#    brand/categoryのいずれかが欠損している商品はk-core計算より前に除外される
#    （`--allow-incomplete-metadata`を指定すると除外しない）。
#    selected_user_ids.txt / selected_product_ids.txt を
#    <output_dir>/kcore_selection に書き出す。
python3 kg_build/select_kcore.py --k 14

# 2. ベースグラフ（Product/User/Review/Category/Brand）を、ステップ1の k-core
#    選定範囲に絞って構築する。計算のためにレビューファイル全体を読み込むため、
#    最も時間のかかるステップである（Video_Games で数分程度）。
python3 kg_build/build_base_graph.py

# 3. 属性抽出（商品メタデータ + レビュー内の言及）。
#    --product-ids-file を指定すると、ステップ2でベースグラフに実際に選定された
#    商品にのみ抽出範囲を絞れる — 実際に LLM 呼び出しを行う場合は必ず指定すること。
#    指定しないと、約1,500件の k-core 商品だけでなく生カタログ全体に対して抽出が
#    走ってしまう。
python3 kg_build/extract_product_attributes.py --resume --product-ids-file kg_output/video_games/nodes_products.csv

#    レビュー内の言及は商品数ではなくレビュー数（デフォルトの k=14 コアで約5.6万件）に
#    比例してスケールする — 最後まで処理し切るものというより、サンプルに対する
#    ベストエフォートのエンリッチメントと捉える。--resume で後からサンプルを
#    拡張できる:
python3 kg_build/extract_review_mentions.py --resume --limit 5000 --min-text-len 60 --batch-size 15

# 4.（任意）上記2つの独立した抽出パスによって生じた attr_type/value の同義語を
#    正規化する（例: "item_form" と "texture"）
python3 kg_build/canonicalize_attributes.py

# 5. 抽出結果を統合して属性ノード/エッジ CSV を生成する
#    （ステップ4の正規化マップを適用し、nodes_products.csv に含まれない
#    商品の属性があれば除外する）
python3 kg_build/build_attribute_graph.py

# 6. Bolt 経由ですべて（ベースグラフ + 属性、CSV が存在する場合）をインポートする
python3 kg_build/import_kg_to_neo4j.py

# 7.（任意）メタデータから Product.image_url を追加（UI での商品サムネイル表示を有効化）
#    し、Product.title を日本語に翻訳し（Product.title_ja）、および／または Review.title/text を
#    日本語に翻訳する（Review.title_ja/text_ja。lang=ja の場合の GET /products/{id}/reviews で
#    使用）。--reviews-ja は helpful_vote の降順で優先順位付けする（実際に表示される可能性が
#    最も高いレビューから処理する）— LLM の予算に応じて --reviews-limit と組み合わせること。
#    規模を拡大した後に再実行しても安全 — 未翻訳の商品／レビューのみが対象になる。
python3 kg_build/backfill_display_fields.py --images --titles-ja --reviews-ja --reviews-limit 2000
```

k を大きくするほど密（1ユーザーあたりの相互作用数が多い）だがユーザー・商品数は少ないグラフになる。再選定する場合は再度ステップ1から実行する（選定が変わるため、ステップ2以降も再実行が必要）。

ステップ3の `--provider`/`--model` は `config.yaml` の `llm.provider`/`llm.model` がデフォルト値になる。上書きする場合は明示的に指定する。

`extract_product_attributes.py` は常に、まずメタデータの `details` フィールドに対する
ゼロコストでジャンル非依存なルールベースのパスを実行し（決定的処理で LLM 呼び出しなし、
手動管理のキーマッピングも不要）、その後 title/features/description に対して LLM を実行する。
各商品のルールベースで得られた属性は `known_attributes` として LLM に渡されるため、
同じ事実を再抽出するのではなく、本当に新しい情報だけを抽出できる。LLM を完全にスキップし、
すべての商品についてゼロコストで属性を抽出するには:
```bash
python3 kg_build/extract_product_attributes.py --rule-only --limit -1
```
予算の都合で LLM を使う（コストのかかる）パスを小さな `--limit` でしか実行できない場合でも、
ルールベースの属性でカタログ全体をカバーしたいときに有用。

### 5. オフライン評価の実行

任意だが、API に触れる前、インポート直後に実行することを推奨する — 稼働中の API サーバは
不要で、（`app/api/recommender.py` の `Recommender` 経由で）Neo4j への接続さえあれば実行できる:

```bash
# まずは小さいサンプルで簡易チェック
python3 eval/eval_offline.py --cutoffs 10 20 50 --sample 100

# 対象となる全ユーザに対するフルの leave-one-out 評価
python3 eval/eval_offline.py --cutoffs 10 20 50 --resume
```

実行前にデータ健全性チェック（商品・ユーザー・レビュー数、価格/画像/評価のカバレッジ）を
出力したうえで、RATED ベースのパーソナライゼーション（Text2Cypher 経由の `recommend_home`）
を2つのベースライン — Item-KNN（ユーザー×アイテムの評価行列に対するコサイン類似度）と
Popularity（rating_count・avg_ratingによる静的ランキング）— と比較する。**カットオフ**とは
HR@K/NDCG@K の K のこと — 上位いくつの推薦を「見つけられたか」の判定に使うかを表す:
カットオフ10なら保持しておいた商品が上位10件に入ったかを問い、カットオフ50ならその手法に
より多くの余地を与えることになる。`--cutoffs`（デフォルト10/20/50）は、これらすべての
カットオフについて leave-one-out の HR@K/NDCG@K を同時に算出する（最大のカットオフ件数分の
結果を一度だけ取得し、そこから切り詰める）。各対象ユーザの直近の★4以上の評価をターゲットとして
保持しておき、そのエッジ（およびそれ以降に評価されたもの）を一時的に取り除いた上で、3手法
すべてがその保持しておいた商品を top K 内に再びランクインさせられるかを試す。HR@K/NDCG@K に
加えて、サマリには手法ごとの `catalog_coverage@k`/`avg_rating@k`（最小カットオフでの値）も
それ自体独立したメトリクスとして報告される — 単にヒット率だけでなく「同じ人気商品ばかり
勧めていないか」も見える。結果は `eval_results.jsonl` / `eval_results.summary.json` に
書き出される（パスは `--out` で設定可能）。

### 6. 推薦 API を起動する

```bash
uvicorn app.api.main:app --reload
```

あるいは、uvicorn のデフォルト値ではなく `config.yaml` の `api.host`/`api.port` を使う場合:
```bash
python -m app.api.main
```

`http://localhost:8000/docs`（または設定した host/port）を開くと、インタラクティブな Swagger UI が表示される。

### 7. フロントエンド UI を起動する

```bash
cd app/web
npm install
npm run dev
```

`http://localhost:5173` を開く。Vite の開発サーバが `/api/*` を `http://localhost:8000` にプロキシする。

### 8. 実際に試す

1. チャットページを開くと、何も入力しないうちに最初の推薦バッチがすぐに表示される — これは
   チャットのターンではなく（「入力中」バブルは出ない）、軽量なバックグラウンドの
   `POST /recommend/home` フェッチにすぎない。常に選択済みのテストユーザが存在し（後述）、
   評価履歴のないユーザは LLM 呼び出しなしで即座に人気度フォールバックの結果を得る一方、
   履歴のあるユーザはパーソナライズされた結果を得られ、再訪時にはより高速になる —
   下記のキャッシュに関する注記を参照。
2. チャット UI の上部で、ドロップダウン（`TestUserSelect`）から**テストユーザ**を選択する —
   組み込みの「オリジナルテストユーザー」というデモ用 ID（パーソナライゼーションなし。これが
   デフォルト）か、`GET /users/sample` からライブで取得される実際の `user_id` のいずれかを選べる
   （これらは現在のグラフで3件以上の評価を持つ実ユーザなので、選ぶと実際にパーソナライズされた
   結果／ホーム推薦が表示される）。
3. 自然言語（日本語または英語）でクエリを入力する。例: 「小学生の子供と一緒に遊べる協力プレイのSwitchゲームが欲しい」
   や "a co-op couch game for the PS5 that's fun for kids and adults together" など。
4. アシスタントは確認の質問をするか（回答するか、「こだわらない」／"no preference" を選んで
   スキップできる）、十分な手がかりが揃っていればそのまま検索に進む。
5. 推薦結果には、LLM による一文の `explanation`（UI の現在の言語で記述される。上部の「日本語」/"EN"
   トグルで切り替え）が表示され、開発モードでは一致した属性と生成された Cypher の生データ
   （`intent.cypher`）も表示されるため、あらゆる推薦理由がブラックボックスにならず検証可能である。
   Text2Cypher の生成・実行が失敗した場合、またはクエリが正当に何にも一致しなかった場合は、
   空の画面を表示する代わりに人気の高評価商品にフォールバックする（レスポンスの `fallback: true`）。
6. 「レビューを見る」を開く、または「Amazon.comで見る」をクリックすると、`/behavior/view` 経由で
   元の `search_id` に紐づいた `VIEWED` エッジが記録される。`_get_dynamic_few_shot()` はこれを
   （`SearchLog` と結合して）読み戻し、このユーザの次のプロンプトを構築する際に、クリックに
   つながった過去のクエリを優先する。
7. 履歴のあるテストユーザがホーム推薦を読み込んだ後、タブを切り替えたり閉じたりすると、
   `POST /recommend/home/warm` へのビーコンが発火し、バックグラウンドでそのユーザの
   パーソナライズされたクエリを再生成してキャッシュする。次回ページを開いたとき
   （1時間のキャッシュ TTL 以内であれば）、`/recommend/home` は LLM を待たずにそのキャッシュから
   即座に結果を返す。
8. **General mode** をトグルすると、`user_id` を付けずに現在のリクエストを送信できる
   （まったく新しい匿名ユーザと同じコードパス、すなわち人気度フォールバック） — 選択中の
   テストユーザを変えずに、「自分向けにパーソナライズされた結果」と「見知らぬ人が見る結果」を
   切り替えられる。**Clear history** は `POST /users/{user_id}/clear_history` 経由で選択中の
   ユーザの `VIEWED`/`SearchLog` 履歴を削除する — `RATED`（データセット由来の評価履歴で、
   パーソナライゼーションの基盤）には一切手を加えない。

パイプラインのステップ4（Neo4j インポート）が実行されていない場合、または Neo4j に到達できない
場合、`/health` は `ok` を返し続けるが `/recommend`/`/chat` の呼び出しは失敗する —
まず API プロセスの stderr 出力を確認すること。

## API の使い方

### `POST /recommend`

自然言語のクエリを受け取る。LLM がグラフに対して1本の Cypher クエリを生成し、その結果を返す。
パーソナライゼーションは、`user_id` が実際の `RATED`／属性履歴を持つユーザを指している場合にのみ
有効になる — 履歴のない `user_id` は匿名リクエストと同様に扱われる（`$uid` はバインドされない
ため LLM はそれを参照できない。バリデータは、`$uid` がバインドされていないのにそれを参照する
生成 Cypher、または `$uid` を使わずリテラルの `user_id` 文字列をハードコードする生成 Cypher を
拒否する）。

**リクエスト:**
```json
{
  "query": "I have dry and sensitive skin, looking for a gentle face moisturizer with hyaluronic acid, preferably fragrance-free",
  "user_id": null,
  "limit": 10,
  "lang": "en"
}
```

**レスポンス（抜粋）:**
```json
{
  "query": "...",
  "mode": "search",
  "intent": {
    "cypher": "MATCH (p:Product)-[r:HAS_ATTRIBUTE]->(a:Attribute) WHERE ... RETURN p.product_id AS product_id, ... ORDER BY score DESC LIMIT $limit",
    "cypher_explanation": "Finds fragrance-free moisturizers matched to dry/sensitive skin with hyaluronic acid"
  },
  "recommendations": [
    {
      "product_id": "B0...",
      "title": "...",
      "display_title": null,
      "image_url": "https://m.media-amazon.com/images/...",
      "price": 18.5,
      "avg_rating": 4.5,
      "rating_count": 328,
      "score": 2.85,
      "matched_attrs": [
        {"attr_type": "skin_type", "value": "dry"},
        {"attr_type": "ingredient", "value": "hyaluronic acid"}
      ],
      "explanation": "Matches dry/sensitive skin and contains hyaluronic acid; fragrance-free"
    }
  ],
  "search_id": "uuid",
  "fallback": false
}
```

`lang`（"ja" | "en"、デフォルトは "en"）は、トップレベルの `intent.cypher_explanation` と各推薦
結果の `explanation` の両方の言語を制御する — LLM はどちらも指定された言語で書くよう指示されて
いる（プロンプト内の few-shot の例は説明のためだけに英語になっている）。また、
`backfill_display_fields.py --titles-ja` が実行済みであれば、`lang="ja"` の場合は
キャッシュされた日本語訳を `display_title` にも格納する（それ以外は `null` で、フロントエンドは
`title` にフォールバックする）。

リトライ後も Cypher の生成・実行に失敗した場合、**または生成されたクエリの実行自体は成功したが
0件だった場合**は、空の結果を表示する代わりに人気度ベースのクエリにフォールバックする
（`fallback: true`）。

### `POST /recommend/home`

クエリテキストなしの行動ベース推薦（`user_id` は必須、`lang` は上記同様に任意）。`RATED`／属性
履歴のないユーザに対しては LLM を完全にスキップし、人気の高評価商品を Neo4j から直接返す
（LLM 呼び出しなし。データベースの往復以外に実質的なレイテンシは発生しない） — 組み込みの
テストユーザは履歴なしから始まるため、これはユーザが何も入力する前に表示される初期の推薦に
使われるパスである。履歴のあるユーザについては、初回呼び出し時に LLM がパーソナライズされた
Cypher クエリを生成し、結果はサーバ側でキャッシュされる（`user_id`＋`lang`＋`limit` ごとに
1時間の TTL）— それ以降の呼び出しはそのキャッシュから即座に返る。ユーザが尋ねる前にどのように
キャッシュが事前に埋められるかについては、下記の `/recommend/home/warm` を参照。

### `POST /recommend/home/warm`

Fire-and-forget 方式: `/recommend/home` と同じボディを受け取り、常に即座に `204` を返す。
`/recommend/home` と同様のキャッシュ生成処理をバックグラウンドでトリガーするが、その完了を
待たず、`SearchLog` エントリも書き込まない（そのため、タブを何度切り替えても検索履歴が
埋もれることはない）。Web UI は `visibilitychange`/`pagehide` 時に `navigator.sendBeacon`
経由でこれを呼び出すため、ユーザがアプリを再度開く頃には、たいていパーソナライズされた
ホーム推薦がすでにキャッシュされている。

### `POST /behavior/view`

ユーザが商品を閲覧したことを（`user_id`、`product_id`、任意の `search_id`とともに）`VIEWED`
エッジとして記録し、パーソナライゼーションのシグナルとして利用する。Web UI は、テストユーザが
推薦カードの「Amazon.comで見る」をクリックした際にこれを呼び出す。

### `POST /chat`

対話型推薦の1ターンを実行する。各ターンで LLM には、グラフに実際に存在する属性タイプ（Neo4j
から一度クエリしてキャッシュされたもの）と `config.yaml` のジャンルが渡され、構造化レスポンス
内の `action`/`filled_slots` によって、さらに確認の質問をするか検索に進むかを自ら決定する。
これにより、ロードされているカタログ／ジャンルが何であっても質問の流れが適応し、ハードコード
されたカテゴリや質問テンプレートは存在しない。Python 側は質問数のハードキャップ
（`MAX_QUESTIONS = 5`）のみを強制し、LLM 呼び出し自体が失敗した場合は即座に検索へ
フォールバックする。検索がトリガーされると、`/recommend` と同じ Text2Cypher パスに委譲する。

```json
{
  "messages": [
    {"role": "user", "content": "小学生の子供と一緒に遊べる協力プレイのSwitchゲームが欲しい"}
  ],
  "limit": 8,
  "lang": "ja",
  "user_id": null
}
```

レスポンスは以下のいずれかになる:
- `action: "ask"` — 1つの質問とクイックリプライの選択肢（`search_id: null`）
- `action: "search"` — `preference_summary`、`intent`（cypher/explanation）、推薦結果、
  および `search_id`（後の `/behavior/view` 呼び出しをこの検索に紐づけられるようにする）

### `GET /users/sample`

認証システムなしでパーソナライゼーションをデモするために、評価履歴を持つ実際の `user_id` を
いくつか返す。

### `GET /products/{product_id}/reviews`

商品の上位レビューを、helpful vote（役に立った票数）の多い順に返す。`lang`（"ja" | "en"、
デフォルトは "en"）は、存在する場合は `Review.title_ja`/`text_ja`（`backfill_display_fields.py
--reviews-ja` によるもの）を優先し、存在しない場合は元のテキストにフォールバックする。

### `POST /users/{user_id}/clear_history`

このユーザの `VIEWED` と `SearchLog`/`SEARCHED` の履歴（およびそれらから生成された
キャッシュ済みホーム推薦があれば、それも含む）を削除する。`RATED` — データセット由来の評価
履歴であり、パーソナライゼーションの実際の基盤 — には一切手を加えない。これはデモセッション中に
蓄積された行動／検索ログをリセットするだけのものである。

## データ規模

データ規模は `select_kcore.py` の2つの仕組みで制御される: 1つはメタデータ完全性フィルタ
（デフォルトON — `price`/`avg_rating`/`rating_count`/`description`/`brand`/`category`のいずれかが
欠損している商品を最初に除外する。`Video_Games`では生のレビューエッジの約31%がこれで除外され、
そのほとんどは`price`欠損が原因。`--allow-incomplete-metadata`で無効化可能）、もう1つは k-core
のサイズ（`--k`、デフォルトは14）— 残った中で、すべてのユーザ・商品が少なくとも k 件の相互作用を
持つ、最大の二部グラフの部分グラフである。この2つによって、グラフ内のすべてのユーザ／商品が
「メタデータが揃っていて」かつ「パーソナライゼーションとオフライン評価にとって意味のある十分な
履歴を持つ」ことが保証される（素朴な上位N商品サンプリングでは、ほとんどのユーザが1件の評価しか
持たなくなってしまうのとは対照的である）。`<output_dir>/kcore_selection/kcore_summary.json` には、
与えられた k に対する結果のユーザ／アイテム／エッジ数が記録される。`build_base_graph.py` を
実行した後は、実際にインポートされた件数が `kg_output/<output_dir>/build_summary.json` に書き出される。

k を大きくするとグラフは急速に縮小する（線形カットではなく反復的な二部グラフの剥ぎ取りである
ため）が、密度は増す — 生き残ったユーザ／商品あたりの相互作用数が増え、これはパーソナライゼー
ションとオフライン評価の質の両方にとって重要である。`Video_Games` での実測値（デフォルトの
メタデータ完全性フィルタ適用後）:

| k | ユーザ数 | アイテム数 | エッジ数 | ユーザあたり平均エッジ数 | アイテムあたり平均エッジ数 |
|---|---|---|---|---|---|
| 8 | 14,428 | 5,815 | 195,254 | 13.53 | 33.58 |
| 10 | 6,724 | 3,369 | 112,380 | 16.71 | 33.36 |
| 12 | 2,917 | 1,728 | 57,354 | 19.66 | 33.19 |
| **14（デフォルト）** | **861** | **610** | **18,771** | **21.80** | **30.77** |
| 15+ | — | — | — | — | コアが空になる（k=15 で崩壊） |

14 はクラスプロジェクトの規模として実用上の落とし所である: 意味のあるグラフパスとオフライン
評価には十分な密度がありながら、ベースグラフの構築・LLM 属性抽出・Neo4j インポートがいずれも
ノート PC 上で妥当な時間で完了する程度に小さい。なお、この表はメタデータ完全性フィルタ導入前に
`select_kcore.py`が出していた数値より小さい — 同じkでも、メタデータが不完全な商品（とそれにしか
依存しない相互作用）がk-core計算より前に除外されるため、ユーザ・アイテム数が減っている。

## 今後の展望

- LLM による属性抽出のカバレッジを商品全体に拡大する
- ユーザ間で共有される評価履歴を使った、協調フィルタリングの few-shot パスを追加する
- 複数ステップのグラフ探索エンドポイント（`GET /product/{id}/related`）を追加する
- 推薦理由に対する明示的なフィードバック（`GAVE_FEEDBACK` エッジ／サムズアップ・ダウン UI）は
  一度試みられたが撤去された — `_get_dynamic_few_shot()` の暗黙的なクリックシグナル（`SearchLog`
  と結合した `VIEWED`）が、追加の UI／エンドポイントの表面積を増やすことなく「この検索は
  役に立ったか」という同じニーズをカバーしている
- ホーム推薦のキャッシュ（`Recommender._home_cache`）はプロセスメモリ上にあり、再起動すると
  リセットされ、将来 API を複数インスタンスにスケールした場合には共有されない — それが問題に
  なる場合は、Neo4j や Redis のような共有ストアに移すこと
- 無料枠の LLM プロバイダ（特に Groq）は1日あたりのトークン割り当てが少なく、授業でのデモでも
  すぐに使い切ってしまうことがある — `/recommend`/`/chat` が予期せず `fallback: true` を返し
  始めた場合は、`config.yaml` の `llm.provider` をより割り当ての多い `gemini` に切り替えると
  よい（確認するには API プロセスの stderr で `429`/`rate_limit_exceeded` エラーを探す）
- マージ前に、並行して進んでいた `main` ブランチの評価／UI系の作業と照らし合わせた。以下は
  意図的に取り込んでいない（機械的に移植できるものではなく、まず設計が必要なため）:
  - `main` にあるより細かい行動イベント分類（impression／filter_change／restart等、こちら
    の `VIEWED` エッジ単体より粒度が細かい）と、匿名ブラウザIDによる識別モデル — 後者は
    このブランチで匿名テストユーザーの選択肢を廃止した決定と直接矛盾する
  - カバレッジギャップ分析・discoverable-poolに基づく評価 — `main` 側の実装はこちらのスキーマ
    に無い商品品質・販売可否フィールドに強く依存している
