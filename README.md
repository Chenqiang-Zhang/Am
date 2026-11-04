# Steam ナレッジグラフ検索・推薦システム

**日本語** | [中文](README.zh-CN.md)

## 1. ディレクトリ構成

```text
.
├── README.md, README.zh-CN.md
├── .env.example           環境変数の一覧（.env にコピーして使う）
├── docker-compose.yml     Neo4j（ライブ用、UCSD 用）、ローカル LLM（Ollama）、Python の実行（app）、API、画面
├── Dockerfile             Python の実行用イメージ
├── docker-compose.gpu.yml ローカル LLM に GPU を割り当てる設定
├── build_all.sh           Neo4j と Ollama を起動し、ライブと UCSD のグラフを既定の設定で構築する
├── requirements.txt
├── utils/                 バックエンドと評価で共有する部品
│   ├── settings.py        環境変数の読み込み
│   ├── llm_client.py      LLM クライアント
│   └── graph_reader.py    Neo4j の読み出し（Cypher はここだけ）
├── kg/                    グラフの構築（5節）
│   ├── build.py           実行（--graph ucsd|live）
│   ├── ucsd_download.py   UCSD のファイルのダウンロード
│   ├── ucsd_source.py     UCSD のファイルから、ゲーム・プレイヤー・レビューを作る
│   ├── steam_source.py    Steam API から、ゲーム・プレイヤー・レビューを作る
│   ├── aspect_extraction.py  観点の抽出（LLM）
│   ├── aspect_few_shot.json  抽出のプロンプトに入れる例（9 件）
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
│           ├── pages/             ChatPage.tsx（会話）、RecommendPage.tsx（おすすめ）
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
cp .env.example .env                    # パスワード（8 文字以上）、PLAYER_ID_SALT、LOCAL_UID / LOCAL_GID（`id -u` と `id -g` の値）、LLM のキーを入れる
docker compose up -d                    # ライブ用 Neo4j と UCSD 用 Neo4j
```

Neo4j と Ollama は、ポートを公開しない。compose のネットワークの中から、サービス名（`neo4j-live`、`neo4j-ucsd`、`ollama`）で接続する。中身を見るには、`docker compose exec neo4j-live cypher-shell -u neo4j -p <パスワード>`（UCSD 用は `neo4j-ucsd`）。Aura などを使う場合は、`.env` の `*_NEO4J_URI` を書き換える。

### ローカル LLM（グラフ構築用）

`BUILD_LLM_*` の既定は、ローカルの Ollama（`qwen2.5:7b-instruct`）。

```bash
docker compose --profile llm up -d ollama                      # CPU で起動
OLLAMA_GPU_DEVICE=<GPU の UUID> docker compose -f docker-compose.yml -f docker-compose.gpu.yml --profile llm up -d ollama   # GPU で起動
docker compose --profile llm exec ollama ollama pull qwen2.5:7b-instruct   # モデルの取得（初回のみ）
```

NVIDIA ドライバが 550 未満だと、最新の Ollama は GPU を使えず、CPU で動く。その場合は、`.env` の `OLLAMA_VERSION` に古い版（動作確認済み: `0.11.11`、ドライバ 535）を指定する。

### グラフの構築

既定の設定で、Neo4j と Ollama の起動から、ライブと UCSD のグラフの構築までを、まとめて実行する。Python のコマンドは、`docker compose run --rm app` の中で実行する。

```bash
./build_all.sh                    # 既定: 上位 100 本
./build_all.sh --games 300        # 本数を指定（最大 3,000）
```

`.env` の `OLLAMA_GPU_DEVICE` に GPU の UUID を入れると、Ollama がその GPU を使う。`BUILD_LLM_PROVIDER` が `local` 以外なら、Ollama は起動しない。

手順ごとに実行する場合:

```bash
docker compose run --rm app python -m kg.build --graph live                     # 取得 → 観点の抽出 → Neo4j に保存
docker compose run --rm app python -m kg.build --graph ucsd                     # ダウンロード → 取得 → 観点の抽出 → Neo4j に保存
docker compose run --rm app python -m kg.build --graph live --games 20          # ゲームの本数を指定（既定 100）
docker compose run --rm app python -m kg.build --graph live --steps load        # 手順を指定（download / collect / aspects / load）
docker compose run --rm app python -m kg.build --graph live --steps aspects --workers 8   # 観点の抽出を並列で行う
```

保存先は `.env` の `LIVE_NEO4J_*` と `UCSD_NEO4J_*`。LLM は、対話・おすすめ用（`CHAT_LLM_*`）と、グラフ構築の観点抽出用（`BUILD_LLM_*`）を、別々に設定する。`*_LLM_PROVIDER` は `gemini`、`deepseek`、`groq`、`openai`、`local` から選び、API キーは `GEMINI_API_KEY` などに入れる。ローカルの LLM（Ollama、vLLM など、OpenAI 互換のサーバ）を使う場合は、`*_LLM_PROVIDER=local`、`*_LLM_BASE_URL`（例：`http://ollama:11434/v1`）、`*_LLM_MODEL` を入れる。取得した結果は `data/<ucsd|live>/` に JSONL で保存され、途中から再開できる。

### アプリの起動

```bash
docker compose up -d api web           # バックエンドと UI  http://localhost:5173
```

使うグラフは `.env` の `GRAPH`（`live` または `ucsd`）で選ぶ。UI の dev サーバは `/api/*` を、API のコンテナ（`http://api:8000`）にプロキシする（`vite.config.ts` の `API_PROXY_TARGET`）。ポートを公開するのは、UI だけで、サーバの `127.0.0.1` に限る。手元の PC から見るには、SSH のポート転送（`ssh -L 5173:localhost:5173 <サーバ>`）を使う。

## 3. データ

### 3.1 構成

同じスキーマ（4節）のデータベースを2つ作る。別々の Neo4j インスタンス（Docker コンテナ）で動かし、統合はしない。

| | UCSD データベース | ライブデータベース |
|---|---|---|
| 入力 | UCSD の公開データセット（3.2） | Steam の API（3.3） |
| 対象ゲーム | 所有者数の多い上位 N 本（既定 100） | レビュー数の多い上位 N 本（既定 100） |
| `Player`・`PLAYED` | 載せる（対象ゲームを遊んだユーザ。割合は `UCSD_PLAYER_PERCENT`、既定 100%） | 載せる（レビュー投稿者） |
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
- **`Player`・`PLAYED`**: `australian_users_items` で、対象ゲームを1本でも遊んだことがある（`playtime_forever` > 0）ユーザのうち、`player_id` のハッシュで決めた割合（`kg/constants.py` の `UCSD_PLAYER_PERCENT`、既定 100%）を取り込む（再現できる）。`PLAYED` は、そのユーザの、対象ゲームの遊んだもの（`hours` > 0）。
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
| `Aspect` | `aspect_id`（キー）, `name`, `name_ja` | レビューで語られる評価の観点。固定の 20 個（4.3） |

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
- 観点は次の 20 個に固定する。

| 区分 | 観点 |
|---|---|
| 内容 | `story`, `characters`, `gameplay`, `combat`, `level_design`, `progression`, `content_volume`, `replayability`, `multiplayer`, `humor` |
| 操作・難易度 | `controls`, `difficulty`, `ui`, `customization` |
| 見た目・音 | `graphics`, `sound_music`, `atmosphere` |
| 品質・価値 | `performance`, `stability`, `value_for_money` |

- 観点は、この 20 個に固定する。LLM が、一覧にない観点を返しても、捨てる。
- 日本語名（`name_ja`）は、固定の訳語を付ける（`kg/aspect_extraction.py` の `SEED_ASPECTS`）。
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
| 4 | `Player`・`PLAYED` の取り込み | 対象ゲームを遊んだユーザ（割合は `UCSD_PLAYER_PERCENT`、既定 100%。`player_id` のハッシュで決める） | レビューの投稿者 |
| 5 | 観点の抽出 | 共通 | 共通 |
| 6 | Neo4j への取り込み | 共通 | 共通 |

1. **ゲームの選定**: 対象ゲームの `appid` の一覧を、ファイルに保存する。以降の手順は、この一覧だけを見る。
2. **ゲーム・タグ・開発元の取り込み**: `Game`、`Tag`（`Genre` / `Feature` / `UserTag`）、`Studio` と、`HAS_TAG`（`UserTag` は `rank` 付き）、`DEVELOPED_BY`、`PUBLISHED_BY` の元データを作る。キーは 4.4 の規則で付ける。
3. **レビューの取り込み**: `Review` と `ABOUT` の元データを作る。ライブは、最初は少量とし、同じ `steamid` が複数のゲームに現れる割合を測ってから増やす。
4. **`Player`・`PLAYED` の取り込み**: `player_id` を、ソルト付きハッシュで作る（ソルトは環境変数）。`PLAYED` は、遊んだもの（`hours` > 0）だけを作る。`WROTE` は、レビューと `Player` が対応するものを作る。
5. **観点の抽出**: レビュー本文を LLM に渡し、当てはまる観点を選ばせる。結果は `MENTIONS` になる。
   - 観点は、4.3 の 20 個に固定する。一覧にない観点は、捨てる。
   - 抽出結果はレビューごとにファイルへ保存し、途中から再開できる。
   - レビューは、1 件ずつ LLM に渡す。プロンプトには、観点の意味と、例（9 件、`kg/aspect_few_shot.json`）を入れる。結果が返らなかったレビューは、観点なしとして記録する。
   - 抽出の対象は、30 文字以上のレビューのうち、1 ゲームあたり 10 件までとする。役立ち票の多い順、同じなら長い順に選ぶ。
   - `--workers N` で、N 件を同時に LLM へ渡す。Ollama 側の同時処理数は、`.env` の `OLLAMA_NUM_PARALLEL`（既定 8）。
6. **Neo4j への取り込み**: 手順 1〜5 の結果（JSONL）を `MERGE` で取り込む。先に制約とインデックス（4.7）を作る。

生成物（JSONL）は `data/<ucsd|live>/` に置き、git の対象外にする。

## 6. 検索・対話

暫定の設計。

## 7. 特定ユーザ向けの静的なおすすめ

ユーザが遊んだゲームから、グラフの経路をたどっておすすめを作る。Cypher は、固定のパラメータ付きの読み取り専用のものだけを使い、LLM は使わない（`app/api/static_recommend.py`）。

### 7.1 記号

| 記号 | 意味 |
|---|---|
| u | おすすめを作るユーザ |
| s | シード。u が遊んだゲーム（評価では、隠したゲームを除く） |
| g | 候補。u が遊んでいないすべてのゲーム（評価で隠したゲームも含む） |
| w_s | シードの重み。`ln(1 + s のプレイ時間)` |
| deg(g) | g を遊んだプレイヤー数（u を除く） |
| co(s, g) | s と g の両方を遊んだプレイヤー数（u を除く） |

人数は、すべて u を除いて数える。評価で隠したゲームの `PLAYED` も数えないので、その辺をグラフから消した場合と同じ結果になる。

### 7.2 経路ごとのスコア

| 経路 | たどる経路 | スコア S(g) |
|---|---|---|
| 共プレイ（`peer`） | s ← `PLAYED` ← 他のプレイヤー → `PLAYED` → g | `Σ_s w_s · co(s, g) / √(deg(s) · deg(g))` |
| タグ（`tag`） | s → `HAS_TAG` → タグ ← `HAS_TAG` ← g | `Σ_t weight(t) / √n(g)` |
| 開発元・販売元（`studio`） | s → `DEVELOPED_BY` / `PUBLISHED_BY` → `Studio` ← g | `Σ_s w_s · r / ln(2 + m)` |
| 人気（`popular`） | 他のプレイヤー → `PLAYED` → g | `ln(1 + deg(g))` |

- 共プレイ: シードは、プレイ時間の長い上位 10 本とする（タグと開発元・販売元では、シードを全部使う）。共有するゲームの数に下限は設けない（1 本でも数える）。w_s を 1 にし、シードを全部使うと、u を除いた Item-KNN（コサイン類似度の和）と同じになる。
- タグ:
  - ジャンルとユーザタグを使い、機能（`Feature`）は使わない。使う辺は `rank` が 10 以下か、`rank` のないものとし、weight の IDF と、g がタグを持つかの判定で、同じ辺を使う。
  - 名前が同じジャンルとユーザタグ（`genre:action` と `usertag:action` のように、`tag_id` の `:` より後ろが同じもの）は、1 つのタグとして扱う。
  - `weight(t) = pref(t) · ln(N / df(t))`。pref(t) は t を持つシードの w_s の和、N は全ゲーム数、df(t) は t を持つゲーム数。weight の大きい上位 30 個のタグを使う。
  - n(g) は、g の持つタグの数（同じ規則で数える）。
- 開発元・販売元:
  - r は、s と g の両方をその会社が開発した（`DEVELOPED_BY`）なら 1.0、それ以外（どちらかが販売のみ）は 0.5。m は、その会社が開発または販売したゲームの数。
  - 1 本のシードは、候補 1 本につき 1 回だけ数える。複数の会社を共有するときは、`w_s · r / ln(2 + m)` が最も大きい会社を使う。

### 7.3 合計と並べ方

`score(g) = Σ_{経路 ∈ {peer, tag, popular}} λ_経路 · S_経路(g) / max S_経路 + λ_studio · S_studio(g) / Σ_s w_s`

- 共プレイ・タグ・人気は候補の中の最大値で割り、開発元・販売元はすべてのシードの重みの合計 Σ_s w_s で割ってから、重み λ を掛けて足す。λ は、共プレイ 1.0、タグ 1.0、開発元・販売元 0.5、人気 0.1 とする（検証用のユーザで調整する前の値）。λ を 0 にすると、その経路は使わない。
- 同じ会社の作品は S_studio が同じなので、最大値で割ると、u と最も強く結び付いた会社の作品がすべて満額の λ を得てしまうが、Σ_s w_s で割ると、u のプレイ履歴のうち、その会社の作品が占める割合（r / ln(2 + m) を掛けたもの）になる。比較のため、最大値で割る旧規則も `combine(..., studio_normalization="max")` で使える。
- score の大きい順に並べ、同じなら appid の昇順にする（小数点以下 12 桁まで同じなら、同じとみなす）。同じユーザには、毎回同じ結果を返す。
- どの経路でもスコアの付かないゲームは、件数が足りないときに、遊んだ人数の多い順（同じなら appid 順）で後ろに足す。
- 遊んだゲームのない（評価で全部を隠した）ユーザは、人気だけで並べる。`player_id` を指定しないときは、`played_count` の多い順にする。

### 7.4 根拠と説明文

各おすすめに、スコアを生んだ経路の実例（`evidence`）と、経路ごとのスコア（`contributions`。合計が `score`）を付ける。実例は、経路ごとに寄与の大きい順で 3 件までとし、`share` はスコアに占める割合。

| 経路 | 実例（`evidence`） | 説明文の例 |
|---|---|---|
| 共プレイ | シードと co(s, g) | 『Portal 2』を遊んだ他のプレイヤーのうち 12人が、このゲームも遊んでいます |
| タグ | タグと、それを持つシード（w_s が最大のもの） | 『Portal 2』と共通するタグ: パズル、物理／『Terraria』と共通するタグ: サンドボックス |
| 開発元・販売元 | 会社、シード、両方の役割（開発元か販売元か）、m | 『Half-Life 2』の開発元 Valve が開発したゲーム |
| 人気 | deg(g) | 他のプレイヤー 340人が遊んでいるゲーム |

- 説明文は、人気以外で寄与の最も大きい経路の実例だけから作り、実例にない名前や数は書かない。タグには、それぞれ、そのタグを持つシードを添える。会社は、開発元か販売元かを書く。人気の実例しかないときだけ、人気を理由にする。
- `eval/explanation_check.py` は、根拠の人数・タグの所属・会社の役割を、別に書いた読み取り専用の Cypher で確かめ、確かめられた割合（根拠の有効率）を出す。

### 7.5 実装

- 1 ユーザにつき、経路ごとに Cypher を実行する（`utils/graph_reader.py` の `co_play_scores`、`tag_concept_scores`、`studio_scores`、`game_popularity`）。各経路は、スコアと、そのスコアを生んだ実例を一緒に返す。開発元・販売元と人気は 1 本ずつ。
- 共プレイは、シードの名前と人数を読む小さな Cypher 1 本と、シード 64 本ごとに数える Cypher 1 本を実行する。シードごとの co(s, g) を整数のビット欄に詰めて数えるので、数える Cypher 1 本につき、他のプレイヤーを 1 回だけたどる。
- タグは、weight の大きい上位 30 個のタグの表（weight と、そのタグを持つシード）を作る Cypher 1 本と、候補ごとに、表のどのタグを持つかと n(g) を返す Cypher 1 本を実行する。
- 共プレイとタグの項は Python で足す。Cypher の `sum()` と同じく大きい項から順に足すので、結果は、素直に書いた 1 本の Cypher と同じになる。それらの Cypher は `eval/check_reader_equivalence.py` に基準として残し、実際のグラフで照合する。
- `collect_path_scores()` の結果は JSON で保存でき、`combine()` で λ を変えて並べ直せる。API と評価は、同じ `combine()` を使う。
- シードの重み（ln(1 + プレイ時間) か 1）、共プレイのシードの数（10 本か全部）、λ は、検証用のユーザで決める（`SeedScheme`、`DEFAULT_PATH_WEIGHTS`）。
- 改良前の方法（v0）は、比較のため `app/api/static_recommend_v0.py` に残す（`python -m eval.evaluate --rankers ... graph_v0`）。

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

`docker compose run --rm app python -m eval.evaluate --users N --k 10 20 50 --runs R`。ユーザごとに、遊んだゲームから 1 本を隠し、そのゲームが上位 K 件に入るかを測る。`--runs` 回（既定 5）、ユーザと隠すゲームを選び直して繰り返し、各指標を 平均 ± 標準偏差 で表示する。

- 使うグラフは、`.env` の `GRAPH` に関わらず、常に UCSD とする。
- 人気順と Item-KNN は、評価するユーザ自身の記録を除いて作る（隠した 1 本が、人気や類似度に入らないようにする）。
