# Steam 知识图谱检索・推荐系统

[日本語](README.md) | **中文**

## 1. 目录结构

```text
.
├── README.md, README.zh-CN.md
├── .env.example           环境变量一览（复制为 .env 使用）
├── docker-compose.yml     Neo4j（实时用、UCSD 用）、本地 LLM（Ollama）
├── docker-compose.gpu.yml 为本地 LLM 分配 GPU 的设置
├── requirements.txt
├── utils/                 后端与评估共用的部件
│   ├── settings.py        读取环境变量
│   ├── llm_client.py      LLM 客户端
│   └── graph_reader.py    Neo4j 读取（Cypher 只写在这里）
├── scripts/
│   └── build_all.sh       启动 Neo4j 和 Ollama，并按默认设置构建实时和 UCSD 的图
├── kg/                    图的构建（第 5 节）
│   ├── build.py           运行（--graph ucsd|live）
│   ├── ucsd_download.py   下载 UCSD 的文件
│   ├── ucsd_source.py     从 UCSD 文件生成游戏・玩家・评论
│   ├── steam_source.py    从 Steam API 生成游戏・玩家・评论
│   ├── aspect_extraction.py  观点抽取（LLM）
│   ├── neo4j_loader.py    保存到 Neo4j
│   └── ids.py, files.py, http_client.py, constants.py   ID 生成、文件・HTTP 处理、常量
├── app/
│   ├── api/               FastAPI 后端
│   │   ├── main.py        端点
│   │   ├── models.py      请求・响应的类型
│   │   ├── presenters.py  图的行 → API 的类型、图片・商店的 URL
│   │   ├── player_profile.py   传给 LLM 的用户信息（4.8）
│   │   ├── static_recommend.py 静态推荐（第 7 节）
│   │   ├── chat.py        检索・对话（第 6 节）
│   │   └── chat_prompts.py     chat.py 的提示词
│   └── web/               React + Vite + TypeScript 的 UI
│       ├── index.html, package.json, tsconfig.json, vite.config.ts
│       └── src/
│           ├── main.tsx, App.tsx, i18n.tsx, index.css
│           ├── api/client.ts      API 调用（fetch 只写在这里）
│           ├── types/recommend.ts API 的类型（对应 app/api/models.py）
│           ├── pages/ChatPage.tsx 对话与推荐的显示
│           └── components/        聊天、卡片、评论、选项等（CSS Modules）
├── eval/                  离线评估（第 8 节）
│   ├── metrics.py         指标
│   ├── rankers.py         热门度、Item-KNN、图
│   └── evaluate.py        运行
└── data/                  导入用的数据（不纳入 git）
```

## 2. 启动

### 准备 Neo4j

```bash
cp .env.example .env                    # 填入密码（8 位以上）、PLAYER_ID_SALT、LLM 的密钥
docker compose up -d                    # 实时用 Neo4j（7687）和 UCSD 用 Neo4j（7688）
```

Neo4j Browser：实时用为 `http://localhost:7474`，UCSD 用为 `http://localhost:7475`。用户名 `neo4j`，密码为 `.env` 中的值。使用 Aura 等服务时，改写 `.env` 中的 `*_NEO4J_URI`。

### 本地 LLM（图构建用）

`BUILD_LLM_*` 的默认值是本地的 Ollama（`qwen2.5:7b-instruct`）。

```bash
docker compose --profile llm up -d ollama                      # 用 CPU 启动
OLLAMA_GPU_DEVICE=<GPU 的 UUID> docker compose -f docker-compose.yml -f docker-compose.gpu.yml --profile llm up -d ollama   # 用 GPU 启动
docker compose --profile llm exec ollama ollama pull qwen2.5:7b-instruct   # 获取模型（仅首次）
```

### 图的构建

按默认设置，一次性执行从 Neo4j 和 Ollama 的启动到实时和 UCSD 的图的构建。

```bash
pip install -r requirements.txt
scripts/build_all.sh                    # 默认：前 100 款
scripts/build_all.sh --games 300        # 指定数量（最多 3,000）
```

在 `.env` 的 `OLLAMA_GPU_DEVICE` 中填入 GPU 的 UUID，Ollama 就会使用该 GPU。`BUILD_LLM_PROVIDER` 不是 `local` 时，不启动 Ollama。

按步骤执行：

```bash
python -m kg.build --graph live                     # 获取 → 观点抽取 → 保存到 Neo4j
python -m kg.build --graph ucsd                     # 下载 → 获取 → 观点抽取 → 保存到 Neo4j
python -m kg.build --graph live --games 20          # 指定游戏数量（默认 100）
python -m kg.build --graph live --steps load        # 指定步骤（download / collect / aspects / load）
python -m kg.build --graph live --steps aspects --workers 8   # 并行进行观点抽取
```

保存位置为 `.env` 中的 `LIVE_NEO4J_*` 和 `UCSD_NEO4J_*`。LLM 分别设置：对话・推荐用（`CHAT_LLM_*`）和图构建的观点抽取用（`BUILD_LLM_*`）。`*_LLM_PROVIDER` 从 `gemini`、`deepseek`、`groq`、`openai`、`local` 中选择，API 密钥填入 `GEMINI_API_KEY` 等。使用本地 LLM（Ollama、vLLM 等与 OpenAI 兼容的服务）时，填入 `*_LLM_PROVIDER=local`、`*_LLM_BASE_URL`（例：`http://localhost:11434/v1`）、`*_LLM_MODEL`。获取的结果以 JSONL 保存到 `data/<ucsd|live>/`，可以从中途恢复。

### 启动应用

```bash
uvicorn app.api.main:app --reload      # 后端  http://localhost:8000

cd app/web
npm install
npm run dev                            # UI  http://localhost:5173
```

使用的图由 `.env` 中的 `GRAPH`（`live` 或 `ucsd`）选择。UI 的 dev 服务器会把 `/api/*` 代理到 `http://localhost:8000`（`vite.config.ts`）。

## 3. 数据

### 3.1 构成

创建两个使用相同 schema（第 4 节）的数据库。分别运行在不同的 Neo4j 实例（Docker 容器，端口不同）上，不合并。

| | UCSD 数据库 | 实时数据库 |
|---|---|---|
| 输入 | UCSD 公开数据集（3.2） | Steam 的 API（3.3） |
| 目标游戏 | 拥有者数最多的前 N 款（默认 100） | 评论数最多的前 N 款（默认 100） |
| `Player`・`PLAYED` | 导入（玩过目标游戏的用户的 25%） | 导入（评论作者） |
| 导入方式 | 批量 | 仅批量（不做定期更新） |

两个数据库中，`Player` 的标识符（steamid、个人资料名）都以加盐哈希的形式保存（4.4）。不显示在界面上。

### 3.2 UCSD（McAuley 研究室的公开数据集）

| 文件 | 内容 |
|---|---|
| `steam_games.json.gz`（`https://cseweb.ucsd.edu/~wckang/steam_games.json.gz`） | 游戏的元信息（`id`＝appid、`title`、`genres`、`tags`、`specs`、`developer`、`publisher`、`release_date`、`price`） |
| `australian_users_items.json.gz`（`https://mcauleylab.ucsd.edu/public_datasets/data/steam/`） | 每个用户拥有的游戏和游玩时间（`playtime_forever`、`playtime_2weeks`。单位为分钟） |
| `australian_user_reviews.json.gz`（同上） | 每个用户的评论（`recommend`、`posted`、`review`、`helpful`） |

导入：

- **游戏**：从 `australian_users_items` 统计拥有者数，选出前 N 款（默认 100）。游戏的属性取自 `steam_games`。
- **`Player`・`PLAYED`**：在 `australian_users_items` 中，玩过至少 1 款目标游戏（`playtime_forever` > 0）的用户里，导入按 `player_id` 的哈希选出的 25%（可复现）。`PLAYED` 为该用户玩过的目标游戏（`hours` > 0）。
- **`Review`**：`australian_user_reviews` 中，目标游戏・目标用户的评论。

数据由 `kg.build` 下载到 `data/ucsd/`（不纳入 git）。UCSD 的页面没有使用条件的说明，只要求引用论文（Kang & McAuley, ICDM 2018 等）。

### 3.3 实时（Steam API）

均无需密钥。调用次数为每天 10 万次以内。

| 用途 | API |
|---|---|
| 游戏列表（约 19 万条） | `IStoreQueryService/Query` |
| 评论数（用于选取）、用户标签（`tagid` 与按权重排序） | `IStoreBrowseService/GetItems`（可一次传入多个 appid。已确认 93 个） |
| 标签 ID 的名称 | `IStoreService/GetTagList` |
| 名称・发售日・价格・类型・功能・开发商・发行商 | `appdetails` |
| 评论（正文、是否推荐、作者的 steamid、作者在该游戏的游玩时间） | `IUserReviewsService/GetAppReviews` |

导入：

- **游戏**：用 `Query` 取得列表，用 `GetItems` 查评论数，选出前 N 款（默认 100）。
- **`Review`**：每款游戏取“有帮助数”前 15 条和最新 5 条。起初少量获取，测量同一个 `steamid` 出现在多款游戏中的比例后再增加。
- **`Player`・`PLAYED`**：由评论作者生成。`PLAYED` 仅限其评论过的游戏（该游戏的游玩时间）。
- `appdetails` 有获取频率限制，因此花时间慢慢获取。

## 4. 图 schema

采用单一 schema。UCSD 与实时数据不合并，以相同的 schema 保存在不同的数据库中。每次构建的差别，只在于是否导入用户（`Player`）及其历史的边。不导入时，其他节点和边也可以照常使用。

定义的是**UCSD 与实时（Steam API）两边都能得到的信息**，以及由评论正文生成的观点（`Aspect`）。只有一边能得到的信息，不放进 schema。

### 4.1 节点

| 标签 | 属性 | 备注 |
|---|---|---|
| `Game` | `appid`（键）, `name`, `name_ja`, `description`, `description_ja`, `release_date`, `price`, `currency` | `price` 为按货币单位的金额（日元为整数，USD 为小数），免费为 0。`currency` 为 `JPY`（实时）或 `USD`（UCSD）。`description` 为简短说明（英文）。`name_ja`、`description_ja` 为用于显示的日文名称和简短说明。均仅限实时，UCSD 为 `null`。取不到的值为 `null` |
| `Tag` | `tag_id`（键）, `name`, `name_ja` | 同时带有子标签 `Genre` / `Feature` / `UserTag`（见下） |
| `Studio` | `studio_id`（键）, `name` | 开发商与发行商 |
| `Review` | `review_id`（键）, `text`, `recommended`, `votes_up`, `created_at` | `recommended` 为“推荐”（真）／“不推荐”（假） |
| `Player` | `player_id`（键） | steamid（或个人资料名）的加盐哈希 |
| `Aspect` | `aspect_id`（键）, `name` | 评论中谈论的评价观点。准备初始集合，没有合适的观点时由 LLM 添加（4.3） |

**`Tag` 的子标签**

| 子标签 | 内容 | 示例 |
|---|---|---|
| `Genre` | 开发者选择的类型 | RPG、Indie、Simulation |
| `Feature` | 开发者选择的功能 | Single-player、Co-op、Steam Achievements |
| `UserTag` | 由玩家投票决定的标签（约 446 种） | Relaxing、Pixel Graphics、Difficult |

- 一个 `Tag` 节点带有 `Tag` 和上面三者之一，共 2 个标签（Neo4j 的多标签）。不限种类的检索用 `Tag`，指定种类的检索用子标签书写。
- `tag_id` 为 `<种类>:<英文名的小写・下划线>`（例：`genre:rpg`、`usertag:pixel_graphics`）。像类型的 Action 与用户标签的 Action 那样，名称相同但种类不同，则是不同的节点。

### 4.2 关系

| 关系 | 属性 |
|---|---|
| `(Game)-[:HAS_TAG]->(Tag)` | `rank`（仅 `UserTag`。1 为最强。能取到的标签全部导入） |
| `(Game)-[:DEVELOPED_BY]->(Studio)` | — |
| `(Game)-[:PUBLISHED_BY]->(Studio)` | — |
| `(Review)-[:ABOUT]->(Game)` | — |
| `(Player)-[:WROTE]->(Review)` | — |
| `(Player)-[:PLAYED]->(Game)` | `hours`, `hours_2w` |
| `(Review)-[:MENTIONS]->(Aspect)` | — |

- `MENTIONS`：表示评论提到了该观点。不区分好评还是差评。
- `PLAYED`：只保留玩过的（`hours` > 0）。UCSD 由拥有列表生成，实时由评论作者的信息（仅限其评论过的游戏）生成。

### 4.3 观点（`Aspect`）

- 观点由 LLM 从评论正文中抽取。
- 初始集合如下。

| 类别 | 观点 |
|---|---|
| 内容 | `story`, `characters`, `gameplay`, `combat`, `level_design`, `progression`, `content_volume`, `replayability`, `multiplayer`, `humor` |
| 操作・难度 | `controls`, `difficulty`, `ui`, `customization` |
| 画面・音效 | `graphics`, `sound_music`, `atmosphere` |
| 品质・价值 | `performance`, `stability`, `value_for_money` |

- 现有观点都不适用时，由 LLM 添加新观点。名称使用英文・小写・下划线。添加的观点在之后的抽取中也作为现有观点使用。
- 观点是用来查找“提及”的辅助手段。不区分好评还是差评，需要判断时，阅读提及该观点的评论正文。

### 4.4 键的生成规则

| 键 | 规则 |
|---|---|
| `Game.appid` | Steam 的 appid（字符串） |
| `Tag.tag_id` | `<种类>:<英文名的小写・下划线>` |
| `Studio.studio_id` | 名称转小写后，将非字母数字字符替换为 `_` |
| `Aspect.aspect_id` | 观点名（英文，小写・下划线） |
| `Review.review_id` | 实时为 `recommendationid`。UCSD 为 `hash(player_id, appid, posted)` |
| `Player.player_id` | 对 steamid 或个人资料名字符串加盐后的哈希。盐通过环境变量管理 |

### 4.5 设计原则

- 价格・发售日等确定性条件，作为 `Game` 的属性。
- 类型・功能・氛围等语义上的偏好，作为 `Tag` 节点和边。
- 不创建意义薄弱的相似度边。关联通过沿着标签・开发商等路径来求得。
- 取不到的值为 `null`。

### 4.6 各属性的来源

| 节点・属性 | UCSD | 实时（无需密钥的 API） |
|---|---|---|
| `Game.appid`, `name` | `id`, `title` | `steam_appid`, `name` |
| `Game.name_ja` | 无（`null`） | 用日语指定（`l=japanese`）调用 `appdetails` 取得的 `name`。没有官方日文名的游戏，与英文名相同 |
| `Game.description` | 无（`null`） | 用英语指定调用 `appdetails` 取得的 `short_description` |
| `Game.description_ja` | 无（`null`） | 用日语指定调用 `appdetails` 取得的 `short_description` |
| `Tag.name_ja` | 无（`null`） | `GetTagList`（日语指定），类型・功能为 `appdetails`（日语指定）的 `genres`・`categories` |
| `Game.release_date` | `release_date`（约 93% 为 ISO 格式） | `release_date` |
| `Game.price`, `currency` | `price`（数值。Free 等字符串为 0）。货币为 `USD` | `price_overview`（`cc=jp`）的 `final` ÷ 100。`is_free` 为真则为 0。货币为 `JPY` |
| `Tag`（`Genre`） | `genres` | `genres` |
| `Tag`（`Feature`） | `specs` | `categories` |
| `Tag`（`UserTag`）与 `rank` | `tags`（按顺位排列的列表） | `tags`（按权重排序。ID 通过 `GetTagList` 转换为名称） |
| `Studio` | `developer`, `publisher` | `developers`, `publishers` |
| `Review.text` | v1 `review` | `review` |
| `Review.recommended` | v1 `recommend` | `voted_up` |
| `Review.votes_up` | v1 `helpful`（"N of M people…" 的 N） | `votes_up` |
| `Review.created_at` | v1 `posted`（不含年份的字符串为 `null`） | `timestamp_created` |
| `Player.player_id` | v1 `user_id` | 评论的 `author.steamid` |
| `PLAYED.hours`, `hours_2w` | v1 `playtime_forever`, `playtime_2weeks`（分钟 → 小时） | 评论的 `author.playtime_forever`, `playtime_last_two_weeks`（分钟 → 小时） |

### 4.7 约束与索引

- 对键（`appid`, `tag_id`, `studio_id`, `review_id`, `player_id`）加唯一约束。
- 对 `Game.name`、`Game.release_date`、`Tag.name` 加索引。
- 对 `Review.text` 和 `Game.name` 加全文索引。

### 4.8 传给 LLM 的用户信息

用户信息不保存，而是从图（`Player`、`PLAYED`、`Review`、`MENTIONS`、`Tag`）生成固定格式的文本传入。内容如下。

1. 游玩时间最长的前 N 款游戏（名称、时间）
2. 最近 2 周玩过的游戏（`hours_2w` > 0）
3. 偏好的标签（按游玩时间加权，对玩过的游戏的标签进行汇总后的前几名）
4. 自己的评论（游戏名、是否推荐、正文）
5. 自己的评论中提到的观点（`MENTIONS` 的汇总）

评估（第 8 节）时，去掉被隐藏的游戏后生成。

## 5. 构建流程

两个数据库都按相同流程构建。不同的只有步骤 1～4 的输入（第 3 节）。各步骤都可以从头重新做（批量导入）。

| # | 步骤 | UCSD | 实时 |
|---|---|---|---|
| 1 | 游戏的选取 | 按 `australian_users_items` 的拥有者数取前 N 款（默认 100） | 用 `Query` 取列表，按 `GetItems` 的评论数取前 N 款（默认 100） |
| 2 | 导入游戏・标签・开发商 | `steam_games` | `GetItems`、`GetTagList`、`appdetails` |
| 3 | 导入评论 | `australian_user_reviews`（目标游戏・目标用户） | `GetAppReviews`（每款游戏前 15 条＋最新 5 条） |
| 4 | 导入 `Player`・`PLAYED` | 玩过目标游戏的用户的 25%（由 `player_id` 的哈希决定） | 评论作者 |
| 5 | 观点抽取 | 共通 | 共通 |
| 6 | 导入 Neo4j | 共通 | 共通 |

1. **游戏的选取**：把目标游戏的 `appid` 列表保存到文件。之后的步骤只看这个列表。
2. **导入游戏・标签・开发商**：生成 `Game`、`Tag`（`Genre` / `Feature` / `UserTag`）、`Studio`，以及 `HAS_TAG`（`UserTag` 带 `rank`）、`DEVELOPED_BY`、`PUBLISHED_BY` 的原始数据。键按 4.4 的规则赋予。
3. **导入评论**：生成 `Review` 和 `ABOUT` 的原始数据。实时部分起初少量获取，测量同一个 `steamid` 出现在多款游戏中的比例后再增加。
4. **导入 `Player`・`PLAYED`**：用加盐哈希生成 `player_id`（盐为环境变量）。`PLAYED` 只生成玩过的（`hours` > 0）。`WROTE` 生成评论与 `Player` 对应的部分。
5. **观点抽取**：把评论正文交给 LLM，让它选出适用的观点。结果成为 `MENTIONS`。
   - 观点列表从 4.3 的初始集合开始。没有适用的观点时，由 LLM 添加新观点，并在之后的抽取中使用。
   - 添加的观点保存到文件（`aspects.json`），跨运行沿用。
   - 抽取结果按评论保存到文件，可以从中途恢复。
6. **导入 Neo4j**：用 `MERGE` 导入步骤 1～5 的结果（JSONL）。先创建约束和索引（4.7）。

生成物（JSONL、`aspects.json`）放在 `data/<ucsd|live>/`，不纳入 git。

## 6. 检索・对话

暂定的设计。

## 7. 面向特定用户的静态推荐

暂定的设计。

## 8. 离线评估

使用 UCSD 数据库的用户历史，评估静态推荐。

### 8.1 比较的方法

- 热门度排序
- Item-KNN
- 静态推荐的方法（第 7 节）

### 8.2 评估指标

K 取 10、20、50。逐用户计算，再对全部用户取平均。

| 指标 | 定义 |
|---|---|
| HR@K | 被隐藏的游戏进入前 K 名则为 1，否则为 0 |
| NDCG@K | 设被隐藏游戏的名次为 r。r 不超过 K 则为 1 / log2(r + 1)，否则为 0 |
| 覆盖率@K | 在全部用户的前 K 名中至少出现过 1 次的游戏数 ÷ 候选游戏数（不做用户平均） |
| 多样性@K | 对从前 K 名中任选 2 款的所有组合，求标签集合的距离（1 − Jaccard 系数）并取平均 |
| 新颖性@K | 前 K 名游戏的 −log2(p) 的平均值。p 为玩过该游戏的用户数 ÷ 全部用户数 |

### 8.3 评估的运行

`python -m eval.evaluate --users N --k 10 20 50`。对每个用户，隐藏其玩过的游戏中的 1 款，测量该游戏是否进入前 K 名。
