# Steam ナレッジグラフ検索・推薦システム

**日本語** | [中文](README.zh-CN.md)

## 1. ディレクトリ構成

```text
.
├── README.md, README.zh-CN.md
├── .env.example           環境変数の一覧（.env にコピーして使う）
├── docker-compose.yml     Neo4j（ライブ用、UCSD 用）、ローカル LLM（Ollama）
├── docker-compose.gpu.yml ローカル LLM に GPU を割り当てる設定
├── requirements.txt
├── utils/                 バックエンドと評価で共有する部品
│   ├── settings.py        環境変数の読み込み
│   ├── llm_client.py      LLM クライアント
│   └── graph_reader.py    Neo4j の読み出し（Cypher はここだけ）
├── scripts/
│   └── build_all.sh       Neo4j と Ollama を起動し、ライブと UCSD のグラフを既定の設定で構築する
├── kg/                    グラフの構築（5節）
│   ├── build.py           実行（--graph ucsd|live）
│   ├── ucsd_download.py   UCSD のファイルのダウンロード
│   ├── ucsd_source.py     UCSD のファイルから、ゲーム・プレイヤー・レビューを作る
│   ├── steam_source.py    Steam API から、ゲーム・プレイヤー・レビューを作る
│   ├── aspect_extraction.py  観点の抽出（LLM）
│   ├── neo4j_loader.py    Neo4j への保存
│   └── ids.py, files.py, http_client.py, constants.py   ID の生成、ファイル・HTTP の処理、定数
├── app/
│   ├── api/               FastAPI のバックエンド
│   │   ├── main.py        エンドポイント
│   │   ├── models.py      リクエスト・レスポンスの型
│   │   ├── presenters.py  グラフの行 → API の型、画像・ストアの URL
│   │   ├── player_profile.py   LLM に渡すユーザ情報（4.8）
│   │   ├── static_recommend.py 静的なおすすめ（7節）
│   │   ├── chat.py        検索・対話（6節）
│   │   └── chat_prompts.py     chat.py のプロンプト
│   └── web/               React + Vite + TypeScript の UI
│       ├── index.html, package.json, tsconfig.json, vite.config.ts
│       └── src/
│           ├── main.tsx, App.tsx, i18n.tsx, index.css
│           ├── api/client.ts      API 呼び出し（fetch はここだけ）
│           ├── types/recommend.ts API の型（app/api/models.py に対応）
│           ├── pages/ChatPage.tsx 会話とおすすめの表示
│           └── components/        チャット、カード、レビュー、選択肢など（CSS Modules）
├── eval/                  オフライン評価（8節）
│   ├── metrics.py         指標
│   ├── rankers.py         人気順、Item-KNN、グラフ
│   └── evaluate.py        実行
└── data/                  取り込み用のデータ（git の対象外）
```

## 2. 起動

### Neo4j の用意

```bash
cp .env.example .env                    # パスワード（8 文字以上）、PLAYER_ID_SALT、LLM のキーを入れる
docker compose up -d                    # ライブ用 Neo4j（7687）と UCSD 用 Neo4j（7688）
```

Neo4j Browser は、ライブ用が `http://localhost:7474`、UCSD 用が `http://localhost:7475`。ユーザ名 `neo4j`、パスワードは `.env` の値。Aura などを使う場合は、`.env` の `*_NEO4J_URI` を書き換える。

### ローカル LLM（グラフ構築用）

`BUILD_LLM_*` の既定は、ローカルの Ollama（`qwen2.5:7b-instruct`）。

```bash
docker compose --profile llm up -d ollama                      # CPU で起動
OLLAMA_GPU_DEVICE=<GPU の UUID> docker compose -f docker-compose.yml -f docker-compose.gpu.yml --profile llm up -d ollama   # GPU で起動
docker compose --profile llm exec ollama ollama pull qwen2.5:7b-instruct   # モデルの取得（初回のみ）
```

### グラフの構築

既定の設定で、Neo4j と Ollama の起動から、ライブと UCSD のグラフの構築までを、まとめて実行する。

```bash
pip install -r requirements.txt
scripts/build_all.sh                    # 既定: 上位 100 本
scripts/build_all.sh --games 300        # 本数を指定（最大 3,000）
```

`.env` の `OLLAMA_GPU_DEVICE` に GPU の UUID を入れると、Ollama がその GPU を使う。`BUILD_LLM_PROVIDER` が `local` 以外なら、Ollama は起動しない。

手順ごとに実行する場合:

```bash
python -m kg.build --graph live                     # 取得 → 観点の抽出 → Neo4j に保存
python -m kg.build --graph ucsd                     # ダウンロード → 取得 → 観点の抽出 → Neo4j に保存
python -m kg.build --graph live --games 20          # ゲームの本数を指定（既定 100）
python -m kg.build --graph live --steps load        # 手順を指定（download / collect / aspects / load）
python -m kg.build --graph live --steps aspects --workers 8   # 観点の抽出を並列で行う
```

保存先は `.env` の `LIVE_NEO4J_*` と `UCSD_NEO4J_*`。LLM は、対話・おすすめ用（`CHAT_LLM_*`）と、グラフ構築の観点抽出用（`BUILD_LLM_*`）を、別々に設定する。`*_LLM_PROVIDER` は `gemini`、`deepseek`、`groq`、`openai`、`local` から選び、API キーは `GEMINI_API_KEY` などに入れる。ローカルの LLM（Ollama、vLLM など、OpenAI 互換のサーバ）を使う場合は、`*_LLM_PROVIDER=local`、`*_LLM_BASE_URL`（例：`http://localhost:11434/v1`）、`*_LLM_MODEL` を入れる。取得した結果は `data/<ucsd|live>/` に JSONL で保存され、途中から再開できる。

### アプリの起動

```bash
uvicorn app.api.main:app --reload      # バックエンド  http://localhost:8000

cd app/web
npm install
npm run dev                            # UI  http://localhost:5173
```

使うグラフは `.env` の `GRAPH`（`live` または `ucsd`）で選ぶ。UI の dev サーバは `/api/*` を `http://localhost:8000` にプロキシする（`vite.config.ts`）。

## 3. データ

### 3.1 構成

同じスキーマ（4節）のデータベースを2つ作る。別々の Neo4j インスタンス（Docker コンテナ、ポートを分ける）で動かし、統合はしない。

| | UCSD データベース | ライブデータベース |
|---|---|---|
| 入力 | UCSD の公開データセット（3.2） | Steam の API（3.3） |
| 対象ゲーム | 所有者数の多い上位 N 本（既定 100） | レビュー数の多い上位 N 本（既定 100） |
| `Player`・`PLAYED` | 載せる（対象ゲームを遊んだユーザの 25%） | 載せる（レビュー投稿者） |
| 取り込み | 一括 | 一括のみ（定期更新はしない） |

どちらのデータベースも、`Player` の識別子（steamid、プロフィール名）は、ソルト付きハッシュにして保存する（4.4）。画面には出さない。

### 3.2 UCSD（McAuley 研の公開データセット）

| ファイル | 内容 |
|---|---|
| `steam_games.json.gz`（`https://cseweb.ucsd.edu/~wckang/steam_games.json.gz`） | ゲームのメタ情報（`id`＝appid、`title`、`genres`、`tags`、`specs`、`developer`、`publisher`、`release_date`、`price`） |
| `australian_users_items.json.gz`（`https://mcauleylab.ucsd.edu/public_datasets/data/steam/`） | ユーザごとの所有ゲームとプレイ時間（`playtime_forever`、`playtime_2weeks`。分単位） |
| `australian_user_reviews.json.gz`（同上） | ユーザごとのレビュー（`recommend`、`posted`、`review`、`helpful`） |

取り込み:

- **ゲーム**: `australian_users_items` から所有者数を集計し、上位 N 本（既定 100）を選ぶ。ゲームの属性は、`steam_games` から取る。
- **`Player`・`PLAYED`**: `australian_users_items` で、対象ゲームを1本でも遊んだことがある（`playtime_forever` > 0）ユーザのうち、`player_id` のハッシュで決めた 25% を取り込む（再現できる）。`PLAYED` は、そのユーザの、対象ゲームの遊んだもの（`hours` > 0）。
- **`Review`**: `australian_user_reviews` の、対象ゲーム・対象ユーザのレビュー。

データは `kg.build` が `data/ucsd/` にダウンロードする（git の対象外）。UCSD のページには、利用条件の記載はなく、論文の引用が求められている（Kang & McAuley, ICDM 2018 ほか）。

### 3.3 ライブ（Steam API）

いずれもキー不要。呼び出しは 1 日 10 万回まで。

| 用途 | API |
|---|---|
| ゲームの一覧（約 19 万件） | `IStoreQueryService/Query` |
| レビュー数（選定用）、ユーザタグ（`tagid` と重み順） | `IStoreBrowseService/GetItems`（複数の appid をまとめて渡せる。93 件で確認） |
| タグ ID の名前 | `IStoreService/GetTagList` |
| 名前・発売日・価格・ジャンル・機能・開発元・販売元 | `appdetails` |
| レビュー（本文、おすすめ可否、投稿者の steamid、投稿者のそのゲームのプレイ時間） | `IUserReviewsService/GetAppReviews` |

取り込み:

- **ゲーム**: `Query` で一覧を取り、`GetItems` でレビュー数を調べ、上位 N 本（既定 100）を選ぶ。
- **`Review`**: ゲームごとに、参考になった数の上位 15 件と、最新 5 件を取得する。最初は少量とし、同じ `steamid` が複数のゲームに現れる割合を測ってから、増やす。
- **`Player`・`PLAYED`**: レビューの投稿者から作る。`PLAYED` は、レビューしたゲームについてのみ（そのゲームのプレイ時間）。
- `appdetails` は取得頻度の制限があるため、時間をかけて取得する。

## 4. グラフスキーマ

単一のスキーマとする。UCSD とライブは統合せず、同じスキーマで、別々のデータベースに保存する。ユーザ（`Player`）と、その履歴のエッジを載せるかどうかだけが、構築ごとに異なる。載せない場合も、他のノードとエッジはそのまま使える。

定義するのは、**UCSD とライブ（Steam API）のどちらからも得られる情報**と、レビュー本文から作る観点（`Aspect`）である。片方でしか得られない情報は、スキーマに含めない。

### 4.1 ノード

| ラベル | プロパティ | 備考 |
|---|---|---|
| `Game` | `appid`（キー）, `name`, `name_ja`, `description`, `description_ja`, `release_date`, `price`, `currency` | `price` は通貨単位の金額（円は整数、USD は小数）で、無料は 0。`currency` は `JPY`（ライブ）または `USD`（UCSD）。`description` は短い説明（英語）。`name_ja`、`description_ja` は表示用の日本語名と短い説明。いずれもライブのみで、UCSD は `null`。取れない値は `null` |
| `Tag` | `tag_id`（キー）, `name`, `name_ja` | サブラベル `Genre` / `Feature` / `UserTag` を併せ持つ（下記） |
| `Studio` | `studio_id`（キー）, `name` | 開発元と販売元 |
| `Review` | `review_id`（キー）, `text`, `recommended`, `votes_up`, `created_at` | `recommended` は「おすすめする」（真）／「おすすめしない」（偽） |
| `Player` | `player_id`（キー） | steamid（またはプロフィール名）のソルト付きハッシュ |
| `Aspect` | `aspect_id`（キー）, `name` | レビューで語られる評価の観点。初期セットを用意し、当てはまるものがなければ LLM が追加する（4.3） |

**`Tag` のサブラベル**

| サブラベル | 内容 | 例 |
|---|---|---|
| `Genre` | 開発者が選ぶジャンル | RPG、Indie、Simulation |
| `Feature` | 開発者が選ぶ機能 | Single-player、Co-op、Steam Achievements |
| `UserTag` | プレイヤーの投票で決まるタグ（約 446 種） | Relaxing、Pixel Graphics、Difficult |

- 1つの `Tag` ノードが、`Tag` と、上の3つのうち1つの、合わせて2つのラベルを持つ（Neo4j の複数ラベル）。種類を問わない検索は `Tag` で、種類を指定する検索はサブラベルで書く。
- `tag_id` は、`<種類>:<英語名の小文字・アンダースコア>` とする（例：`genre:rpg`、`usertag:pixel_graphics`）。ジャンルの Action とユーザタグの Action のように、名前が同じでも種類が違えば別ノードになる。

### 4.2 リレーションシップ

| 関係 | プロパティ |
|---|---|
| `(Game)-[:HAS_TAG]->(Tag)` | `rank`（`UserTag` のみ。1 が最も強い。取れるタグはすべて取り込む） |
| `(Game)-[:DEVELOPED_BY]->(Studio)` | — |
| `(Game)-[:PUBLISHED_BY]->(Studio)` | — |
| `(Review)-[:ABOUT]->(Game)` | — |
| `(Player)-[:WROTE]->(Review)` | — |
| `(Player)-[:PLAYED]->(Game)` | `hours`, `hours_2w` |
| `(Review)-[:MENTIONS]->(Aspect)` | — |

- `MENTIONS`: レビューが、その観点に言及していることを表す。好意的か否定的かは持たない。
- `PLAYED`: 遊んだもの（`hours` > 0）だけを持つ。UCSD の所有一覧と、ライブのレビュー投稿者の情報（レビューしたゲームのみ）の、どちらからも作る。

### 4.3 観点（`Aspect`）

- 観点は、レビュー本文から LLM が抽出する。
- 初期セットは次のとおり。

| 区分 | 観点 |
|---|---|
| 内容 | `story`, `characters`, `gameplay`, `combat`, `level_design`, `progression`, `content_volume`, `replayability`, `multiplayer`, `humor` |
| 操作・難易度 | `controls`, `difficulty`, `ui`, `customization` |
| 見た目・音 | `graphics`, `sound_music`, `atmosphere` |
| 品質・価値 | `performance`, `stability`, `value_for_money` |

- 既存の観点のどれにも当てはまらない場合に、LLM が新しい観点を追加する。名前は英語・小文字・アンダースコアとする。追加された観点は、以降の抽出でも既存の観点として使う。
- 観点は、言及を探すための補助である。好意的か否定的かは持たず、判断が必要なときは、言及しているレビューの本文を読む。

### 4.4 キーの生成規則

| キー | 規則 |
|---|---|
| `Game.appid` | Steam の appid（文字列） |
| `Tag.tag_id` | `<種類>:<英語名の小文字・アンダースコア>` |
| `Studio.studio_id` | 名前の小文字化で、英数字以外を `_` にしたもの |
| `Aspect.aspect_id` | 観点名（英語、小文字・アンダースコア） |
| `Review.review_id` | ライブは `recommendationid`。UCSD は `hash(player_id, appid, posted)` |
| `Player.player_id` | steamid またはプロフィール名の文字列に、ソルトを付けたハッシュ。ソルトは環境変数で管理する |

### 4.5 設計原則

- 価格・発売日などの確定的な条件は、`Game` のプロパティにする。
- ジャンル・機能・雰囲気などの意味的な好みは、`Tag` ノードとエッジにする。
- 意味の薄い類似度エッジは作らない。関連は、タグ・開発元などの経路をたどって求める。
- 取れない値は `null` とする。

### 4.6 各プロパティの出どころ

| ノード・プロパティ | UCSD | ライブ（キー不要の API） |
|---|---|---|
| `Game.appid`, `name` | `id`, `title` | `steam_appid`, `name` |
| `Game.name_ja` | なし（`null`） | `appdetails` を日本語指定（`l=japanese`）で取得した `name`。公式の日本語名がないゲームは、英語名と同じ |
| `Game.description` | なし（`null`） | `appdetails` を英語指定で取得した `short_description` |
| `Game.description_ja` | なし（`null`） | `appdetails` を日本語指定で取得した `short_description` |
| `Tag.name_ja` | なし（`null`） | `GetTagList`（日本語指定）、ジャンル・機能は `appdetails`（日本語指定）の `genres`・`categories` |
| `Game.release_date` | `release_date`（約 93% が ISO 形式） | `release_date` |
| `Game.price`, `currency` | `price`（数値。Free などの文字列は 0）。通貨は `USD` | `price_overview`（`cc=jp`）の `final` ÷ 100。`is_free` が真なら 0。通貨は `JPY` |
| `Tag`（`Genre`） | `genres` | `genres` |
| `Tag`（`Feature`） | `specs` | `categories` |
| `Tag`（`UserTag`）と `rank` | `tags`（順位順のリスト） | `tags`（重み順。ID は `GetTagList` で名前に変換） |
| `Studio` | `developer`, `publisher` | `developers`, `publishers` |
| `Review.text` | v1 `review` | `review` |
| `Review.recommended` | v1 `recommend` | `voted_up` |
| `Review.votes_up` | v1 `helpful`（"N of M people…" の N） | `votes_up` |
| `Review.created_at` | v1 `posted`（年のない文字列は `null`） | `timestamp_created` |
| `Player.player_id` | v1 `user_id` | レビューの `author.steamid` |
| `PLAYED.hours`, `hours_2w` | v1 `playtime_forever`, `playtime_2weeks`（分 → 時間） | レビューの `author.playtime_forever`, `playtime_last_two_weeks`（分 → 時間） |


### 4.7 制約とインデックス

- キー（`appid`, `tag_id`, `studio_id`, `review_id`, `player_id`）に一意制約を付ける。
- `Game.name`、`Game.release_date`、`Tag.name` にインデックスを付ける。
- `Review.text` と `Game.name` に全文インデックスを付ける。

### 4.8 LLM に渡すユーザ情報

ユーザ情報は保存せず、グラフ（`Player`、`PLAYED`、`Review`、`MENTIONS`、`Tag`）から、決まった形式のテキストにして渡す。内容は次のとおり。

1. プレイ時間の長い上位 N 本（名前、時間）
2. 直近 2 週間に遊んだゲーム（`hours_2w` > 0）
3. 好むタグ（遊んだゲームのタグを、プレイ時間で重み付けして集計した上位）
4. 自分のレビュー（ゲーム名、おすすめ可否、本文）
5. 自分のレビューで言及した観点（`MENTIONS` の集計）

評価（8節）では、隠したゲームを除いて作る。

## 5. 構築パイプライン

どちらのデータベースも、同じ流れで作る。違うのは、手順 1〜4 の入力だけである（3節）。各手順は、最初から作り直せる（一括取り込み）。

| # | 手順 | UCSD | ライブ |
|---|---|---|---|
| 1 | ゲームの選定 | `australian_users_items` の所有者数で上位 N 本（既定 100） | `Query` で一覧、`GetItems` のレビュー数で上位 N 本（既定 100） |
| 2 | ゲーム・タグ・開発元の取り込み | `steam_games` | `GetItems`、`GetTagList`、`appdetails` |
| 3 | レビューの取り込み | `australian_user_reviews`（対象ゲーム・対象ユーザ） | `GetAppReviews`（ゲームごとに上位 15 件＋最新 5 件） |
| 4 | `Player`・`PLAYED` の取り込み | 対象ゲームを遊んだユーザの 25%（`player_id` のハッシュで決める） | レビューの投稿者 |
| 5 | 観点の抽出 | 共通 | 共通 |
| 6 | Neo4j への取り込み | 共通 | 共通 |

1. **ゲームの選定**: 対象ゲームの `appid` の一覧を、ファイルに保存する。以降の手順は、この一覧だけを見る。
2. **ゲーム・タグ・開発元の取り込み**: `Game`、`Tag`（`Genre` / `Feature` / `UserTag`）、`Studio` と、`HAS_TAG`（`UserTag` は `rank` 付き）、`DEVELOPED_BY`、`PUBLISHED_BY` の元データを作る。キーは 4.4 の規則で付ける。
3. **レビューの取り込み**: `Review` と `ABOUT` の元データを作る。ライブは、最初は少量とし、同じ `steamid` が複数のゲームに現れる割合を測ってから増やす。
4. **`Player`・`PLAYED` の取り込み**: `player_id` を、ソルト付きハッシュで作る（ソルトは環境変数）。`PLAYED` は、遊んだもの（`hours` > 0）だけを作る。`WROTE` は、レビューと `Player` が対応するものを作る。
5. **観点の抽出**: レビュー本文を LLM に渡し、当てはまる観点を選ばせる。結果は `MENTIONS` になる。
   - 観点の一覧は、4.3 の初期セットから始める。当てはまるものがなければ、LLM が新しい観点を追加し、以降の抽出で使う。
   - 追加された観点は、ファイル（`aspects.json`）に保存し、実行をまたいで引き継ぐ。
   - 抽出結果はレビューごとにファイルへ保存し、途中から再開できる。
6. **Neo4j への取り込み**: 手順 1〜5 の結果（JSONL）を `MERGE` で取り込む。先に制約とインデックス（4.7）を作る。

生成物（JSONL、`aspects.json`）は `data/<ucsd|live>/` に置き、git の対象外にする。

## 6. 検索・対話

暫定の設計。

## 7. 特定ユーザ向けの静的なおすすめ

暫定の設計。

## 8. オフライン評価

UCSD データベースのユーザ履歴で、静的なおすすめを評価する。

### 8.1 比較する手法

- 人気順
- Item-KNN
- 静的なおすすめの手法（7節）

### 8.2 評価指標

K は 10、20、50 とする。ユーザごとに求め、全ユーザで平均する。

| 指標 | 定義 |
|---|---|
| HR@K | 隠したゲームが、上位 K 件に入っていれば 1、入っていなければ 0 |
| NDCG@K | 隠したゲームの順位を r とする。r が K 以下なら 1 / log2(r + 1)、そうでなければ 0 |
| カバレッジ@K | 全ユーザの上位 K 件に、1 度でも現れたゲームの数 ÷ 候補ゲームの数（ユーザ平均はしない） |
| 多様性@K | 上位 K 件から 2 本を選ぶすべての組について、タグ集合の距離（1 − Jaccard 係数）を求め、平均する |
| 新規性@K | 上位 K 件のゲームの −log2(p) の平均。p は、そのゲームを遊んだユーザ数 ÷ 全ユーザ数 |

### 8.3 評価の実行

`python -m eval.evaluate --users N --k 10 20 50`。ユーザごとに、遊んだゲームから 1 本を隠し、そのゲームが上位 K 件に入るかを測る。
