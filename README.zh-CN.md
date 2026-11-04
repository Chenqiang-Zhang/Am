# Steam 知识图谱检索・推荐系统

[日本語](README.md) | **中文**

## 1. 目录结构

```text
.
├── README.md, README.zh-CN.md
├── .env.example           环境变量一览（复制为 .env 使用）
├── docker-compose.yml     Neo4j（实时用、UCSD 用）、本地 LLM（Ollama）、Python 运行（app）、API、界面
├── Dockerfile             Python 运行用镜像
├── docker-compose.gpu.yml 为本地 LLM 分配 GPU 的设置
├── build_all.sh           启动 Neo4j 和 Ollama，并按默认设置构建实时和 UCSD 的图
├── requirements.txt
├── utils/                 后端与评估共用的部件
│   ├── settings.py        读取环境变量
│   ├── llm_client.py      LLM 客户端
│   └── graph_reader.py    Neo4j 读取（Cypher 只写在这里）
├── kg/                    图的构建（第 5 节）
│   ├── build.py           运行（--graph ucsd|live）
│   ├── ucsd_download.py   下载 UCSD 的文件
│   ├── ucsd_source.py     从 UCSD 文件生成游戏・玩家・评论
│   ├── steam_source.py    从 Steam API 生成游戏・玩家・评论
│   ├── aspect_extraction.py  观点抽取（LLM）
│   ├── aspect_few_shot.json  抽取提示词中的示例（9 条）
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
│           ├── pages/             ChatPage.tsx（对话）、RecommendPage.tsx（推荐）
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
cp .env.example .env                    # 填入密码（8 位以上）、PLAYER_ID_SALT、LOCAL_UID / LOCAL_GID（`id -u` 和 `id -g` 的值）、LLM 的密钥
docker compose up -d                    # 实时用 Neo4j 和 UCSD 用 Neo4j
```

Neo4j 和 Ollama 不公开端口。在 compose 的网络内，用服务名（`neo4j-live`、`neo4j-ucsd`、`ollama`）连接。查看内容用 `docker compose exec neo4j-live cypher-shell -u neo4j -p <密码>`（UCSD 用为 `neo4j-ucsd`）。使用 Aura 等服务时，改写 `.env` 中的 `*_NEO4J_URI`。

### 本地 LLM（图构建用）

`BUILD_LLM_*` 的默认值是本地的 Ollama（`qwen2.5:7b-instruct`）。

```bash
docker compose --profile llm up -d ollama                      # 用 CPU 启动
OLLAMA_GPU_DEVICE=<GPU 的 UUID> docker compose -f docker-compose.yml -f docker-compose.gpu.yml --profile llm up -d ollama   # 用 GPU 启动
docker compose --profile llm exec ollama ollama pull qwen2.5:7b-instruct   # 获取模型（仅首次）
```

NVIDIA 驱动低于 550 时，最新的 Ollama 无法使用 GPU，会改用 CPU 运行。此时在 `.env` 的 `OLLAMA_VERSION` 中指定旧版本（已确认可用：`0.11.11`，驱动 535）。

### 图的构建

按默认设置，一次性执行从 Neo4j 和 Ollama 的启动到实时和 UCSD 的图的构建。Python 命令在 `docker compose run --rm app` 中执行。

```bash
./build_all.sh                    # 默认：前 100 款
./build_all.sh --games 300        # 指定数量（最多 3,000）
```

在 `.env` 的 `OLLAMA_GPU_DEVICE` 中填入 GPU 的 UUID，Ollama 就会使用该 GPU。`BUILD_LLM_PROVIDER` 不是 `local` 时，不启动 Ollama。

按步骤执行：

```bash
docker compose run --rm app python -m kg.build --graph live                     # 获取 → 观点抽取 → 保存到 Neo4j
docker compose run --rm app python -m kg.build --graph ucsd                     # 下载 → 获取 → 观点抽取 → 保存到 Neo4j
docker compose run --rm app python -m kg.build --graph live --games 20          # 指定游戏数量（默认 100）
docker compose run --rm app python -m kg.build --graph live --steps load        # 指定步骤（download / collect / aspects / load）
docker compose run --rm app python -m kg.build --graph live --steps aspects --workers 8   # 并行进行观点抽取
```

保存位置为 `.env` 中的 `LIVE_NEO4J_*` 和 `UCSD_NEO4J_*`。LLM 分别设置：对话・推荐用（`CHAT_LLM_*`）和图构建的观点抽取用（`BUILD_LLM_*`）。`*_LLM_PROVIDER` 从 `gemini`、`deepseek`、`groq`、`openai`、`local` 中选择，API 密钥填入 `GEMINI_API_KEY` 等。使用本地 LLM（Ollama、vLLM 等与 OpenAI 兼容的服务）时，填入 `*_LLM_PROVIDER=local`、`*_LLM_BASE_URL`（例：`http://ollama:11434/v1`）、`*_LLM_MODEL`。获取的结果以 JSONL 保存到 `data/<ucsd|live>/`，可以从中途恢复。

### 启动应用

```bash
docker compose up -d api web           # 后端和 UI  http://localhost:5173
```

使用的图由 `.env` 中的 `GRAPH`（`live` 或 `ucsd`）选择。UI 的 dev 服务器会把 `/api/*` 代理到 API 容器（`http://api:8000`，`vite.config.ts` 的 `API_PROXY_TARGET`）。只公开 UI 的端口，且仅限服务器的 `127.0.0.1`。从本机 PC 查看时，使用 SSH 端口转发（`ssh -L 5173:localhost:5173 <服务器>`）。

## 3. 数据

### 3.1 构成

创建两个使用相同 schema（第 4 节）的数据库。分别运行在不同的 Neo4j 实例（Docker 容器）上，不合并。

| | UCSD 数据库 | 实时数据库 |
|---|---|---|
| 输入 | UCSD 公开数据集（3.2） | Steam 的 API（3.3） |
| 目标游戏 | 拥有者数最多的前 N 款（默认 100） | 评论数最多的前 N 款（默认 100） |
| `Player`・`PLAYED` | 导入（玩过目标游戏的用户。比例为 `UCSD_PLAYER_PERCENT`，默认 100%） | 导入（评论作者） |
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
- **`Player`・`PLAYED`**：在 `australian_users_items` 中，玩过至少 1 款目标游戏（`playtime_forever` > 0）的用户里，导入按 `player_id` 的哈希选出的比例（`kg/constants.py` 的 `UCSD_PLAYER_PERCENT`，默认 100%，可复现）。`PLAYED` 为该用户玩过的目标游戏（`hours` > 0）。
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
| `Aspect` | `aspect_id`（键）, `name`, `name_ja` | 评论中谈论的评价观点。固定的 20 个（4.3） |

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
- 观点固定为以下 20 个。

| 类别 | 观点 |
|---|---|
| 内容 | `story`, `characters`, `gameplay`, `combat`, `level_design`, `progression`, `content_volume`, `replayability`, `multiplayer`, `humor` |
| 操作・难度 | `controls`, `difficulty`, `ui`, `customization` |
| 画面・音效 | `graphics`, `sound_music`, `atmosphere` |
| 品质・价值 | `performance`, `stability`, `value_for_money` |

- 观点固定为这 20 个。LLM 返回列表之外的观点时，予以丢弃。
- 日文名（`name_ja`）使用固定译名（`kg/aspect_extraction.py` 的 `SEED_ASPECTS`）。
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
| 4 | 导入 `Player`・`PLAYED` | 玩过目标游戏的用户（比例为 `UCSD_PLAYER_PERCENT`，默认 100%，由 `player_id` 的哈希决定） | 评论作者 |
| 5 | 观点抽取 | 共通 | 共通 |
| 6 | 导入 Neo4j | 共通 | 共通 |

1. **游戏的选取**：把目标游戏的 `appid` 列表保存到文件。之后的步骤只看这个列表。
2. **导入游戏・标签・开发商**：生成 `Game`、`Tag`（`Genre` / `Feature` / `UserTag`）、`Studio`，以及 `HAS_TAG`（`UserTag` 带 `rank`）、`DEVELOPED_BY`、`PUBLISHED_BY` 的原始数据。键按 4.4 的规则赋予。
3. **导入评论**：生成 `Review` 和 `ABOUT` 的原始数据。实时部分起初少量获取，测量同一个 `steamid` 出现在多款游戏中的比例后再增加。
4. **导入 `Player`・`PLAYED`**：用加盐哈希生成 `player_id`（盐为环境变量）。`PLAYED` 只生成玩过的（`hours` > 0）。`WROTE` 生成评论与 `Player` 对应的部分。
5. **观点抽取**：把评论正文交给 LLM，让它选出适用的观点。结果成为 `MENTIONS`。
   - 观点固定为 4.3 的 20 个。列表之外的观点，予以丢弃。
   - 抽取结果按评论保存到文件，可以从中途恢复。
   - 评论逐条传给 LLM。提示词中包含观点的含义和示例（9 条，`kg/aspect_few_shot.json`）。没有返回结果的评论，记录为无观点。
   - 抽取对象为 30 个字符以上的评论，每个游戏最多 10 条。按有用票数从多到少，相同则按长度从长到短选取。
   - 用 `--workers N` 同时向 LLM 传入 N 条。Ollama 一侧的并发数由 `.env` 的 `OLLAMA_NUM_PARALLEL` 决定（默认 8）。
6. **导入 Neo4j**：用 `MERGE` 导入步骤 1～5 的结果（JSONL）。先创建约束和索引（4.7）。

生成物（JSONL）放在 `data/<ucsd|live>/`，不纳入 git。

## 6. 检索・对话

暂定的设计。

## 7. 面向特定用户的静态推荐

从用户玩过的游戏出发，沿图上的路径生成推荐。只使用固定的、带参数的只读 Cypher，不使用 LLM（`app/api/static_recommend.py`）。

### 7.1 记号

| 记号 | 含义 |
|---|---|
| u | 生成推荐的用户 |
| s | 种子。u 玩过的游戏（评估时去掉被隐藏的游戏） |
| g | 候选。u 没有玩过的所有游戏（包括评估时被隐藏的游戏） |
| w_s | 种子的权重。默认为 1（`SeedScheme(weighting="log_hours")` 时为 `ln(1 + s 的游玩时长)`） |
| deg(g) | 玩过 g 的玩家数（不含 u） |
| co(s, g) | s 和 g 都玩过的玩家数（不含 u） |

人数一律不计入 u。评估时被隐藏游戏的 `PLAYED` 也不计入，因此结果与从图中删除这条边时相同。

### 7.2 各路径的分数

| 路径 | 经过的路径 | 分数 S(g) |
|---|---|---|
| 共玩（`peer`） | s ← `PLAYED` ← 其他玩家 → `PLAYED` → g | `Σ_s w_s · co(s, g) / √(deg(s) · deg(g))` |
| 标签（`tag`） | s → `HAS_TAG` → 标签 ← `HAS_TAG` ← g | `Σ_t weight(t) / √n(g)` |
| 开发商・发行商（`studio`） | s → `DEVELOPED_BY` / `PUBLISHED_BY` → `Studio` ← g | `Σ_s w_s · r / ln(2 + m)` |
| 热门（`popular`） | 其他玩家 → `PLAYED` → g | `ln(1 + deg(g))` |

- 共玩：默认使用全部种子（可用 `SeedScheme` 的 `peer_limit` 只取游玩时长最长的前 N 款；标签和开发商・发行商始终使用全部种子）。不设共同游戏数的下限（共同玩过 1 款也计入）。w_s 取 1 且使用全部种子的默认设置，与不含 u 的 Item-KNN（余弦相似度之和）相同。
- 标签：
  - 使用类型和用户标签，不使用功能（`Feature`）。使用的边为 `rank` 不超过 10 或没有 `rank` 的边；weight 的 IDF 和“g 是否带有该标签”的判断使用同一组边。
  - 名称相同的类型和用户标签（如 `genre:action` 与 `usertag:action`，即 `tag_id` 中 `:` 之后的部分相同）视为同一个标签。
  - `weight(t) = pref(t) · ln(N / df(t))`。pref(t) 为带有 t 的种子的 w_s 之和，N 为全部游戏数，df(t) 为带有 t 的游戏数。使用 weight 最大的前 30 个标签。
  - n(g) 为 g 带有的标签数（按同样的规则计数）。
- 开发商・发行商：
  - 若该公司开发了 s 和 g 两者（`DEVELOPED_BY`），r 为 1.0，否则（任一方仅为发行）为 0.5。m 为该公司开发或发行的游戏数。
  - 对每个候选，1 款种子只计 1 次。共有多个公司时，使用 `w_s · r / ln(2 + m)` 最大的那个。

### 7.3 合计与排序

`score(g) = Σ_{路径 ∈ {peer, tag, popular}} λ_路径 · S_路径(g) / max S_路径 + λ_studio · S_studio(g) / Σ_s w_s`

- 共玩、标签和热门先除以候选中的最大值，开发商・发行商先除以全部种子的权重之和 Σ_s w_s，再乘以权重 λ 后相加。λ 取在验证用户上选出的共玩 1.0、标签 0.1、开发商・发行商 0、热门 0（8.5）。λ 为 0 时，不使用该路径（热门仍用于条数不够时的补充）。
- 同一公司的作品 S_studio 相同，除以最大值会让与 u 关联最强的公司的每部作品都拿到满额的 λ，而除以 Σ_s w_s 后，它就是 u 的游玩记录中该公司作品所占的比例（再乘以 r / ln(2 + m)）。为了比较，除以最大值的旧规则仍可通过 `combine(..., studio_normalization="max")` 使用。
- 按 score 从大到小排列，相同则按 appid 升序（小数点后 12 位相同即视为相同）。对同一用户，每次返回相同的结果。
- 任何路径都没有分数的游戏，在数量不足时，按玩过的人数从多到少（相同则按 appid）补在后面。
- 没有玩过的游戏的用户（评估时全部被隐藏），只按热门排序。不指定 `player_id` 时，按 `played_count` 从多到少排列。

### 7.4 依据与说明文

每条推荐附上产生分数的路径实例（`evidence`）和各路径的分数（`contributions`，合计等于 `score`）。实例按贡献从大到小，每条路径最多 3 条，`share` 为其在分数中所占的比例。

| 路径 | 实例（`evidence`） | 说明文示例（英文界面） |
|---|---|---|
| 共玩 | 种子与 co(s, g) | 12 other players who played Portal 2 also played this game |
| 标签 | 标签，以及带有该标签的种子（锚点带有时为锚点，否则为 w_s 最大的那款，相同则取 appid 较小者） | Shares Co-op, Sci-fi with Portal 2 |
| 开发商・发行商 | 公司、种子、双方的角色（开发商还是发行商）、m | Developed by Valve, the developer of Half-Life 2 |
| 热门 | deg(g) | Played by 340 other players |

- 说明文只根据热门以外贡献最大的路径的实例生成，不写实例中没有的名称或数字。公司注明是开发商还是发行商。人数每 3 位用逗号分隔。只有热门的实例时，才以热门为理由。
- 标签的说明文只写与 1 款种子（锚点）共有的标签。锚点是：把前 30 个标签中 g 带有的那些标签的项 weight(t) / √n(g)，按带有该标签的种子分别相加，和最大的种子（相同则取 w_s 较大者，再取 appid 较小者）。实例中没有锚点的实例时，使用第一个实例的种子。1,000 款以上的游戏带有的标签（Multiplayer、Singleplayer 等），在该种子的实例（最多 3 个）中还有此类以外的标签时不写。
- `eval/explanation_check.py` 用另外编写的只读 Cypher 核对依据中的人数、标签归属和公司角色。主要结果是依据的断言全部成立的推荐所占的比例（逐条推荐的有效率，C5），按断言计算的比例放在辅助表中。

### 7.5 实现

- 每个用户、每条路径执行 Cypher（`utils/graph_reader.py` 的 `co_play_scores`、`tag_concept_scores`、`studio_scores`、`game_popularity`）。各路径同时返回分数和产生该分数的实例。开发商・发行商和热门各 1 条。
- 共玩先执行 1 条读取种子名称和人数的小 Cypher，再每 64 个种子执行 1 条计数的 Cypher。每个种子的 co(s, g) 打包在整数的位字段里计数，因此每条计数的 Cypher 中，每个其他玩家只展开 1 次。位字段的宽度由先读取的人数决定，因此执行期间不能写入图。
- 标签先执行 1 条生成 weight 最大的前 30 个标签的表（weight、带有该标签的种子和 df），再执行 1 条对每个候选返回它带有表中的哪些标签以及 n(g)。锚点只对要推荐的游戏在 Python 里选。
- 共玩和标签的各项在 Python 里相加。与 Cypher 的 `sum()` 一样从大的项开始依次相加，因此结果与直接写成 1 条的 Cypher 相同。这些 Cypher 作为基准保留在 `eval/check_reader_equivalence.py`，在真实图上对照。
- `collect_path_scores()` 的结果可以保存为 JSON，用 `combine()` 改变 λ 后重新排序。API 与评估使用同一个 `combine()`。
- 默认的种子权重为 1，共玩使用全部种子，λ 为 1 / 0.1 / 0 / 0（`SeedScheme`、`DEFAULT_PATH_WEIGHTS`，在验证用户上选出，见 8.5）。由于使用全部种子，玩过的游戏越多的用户，共玩的计算越耗时。
- 改进前的方法（v0）为了比较，保留在 `app/api/static_recommend_v0.py`（`python -m eval.evaluate --rankers ... graph_v0`）。

## 8. 离线评估

在 UCSD 的图上，隐藏用户玩过的 1 款游戏，测量各方法把它排在第几名。流程、比较的行、配置的选法和判定，在验证与测试的结果出来之前就写入 `eval/results/static_plan_v1.json` 并已提交（8.1-8.4 是它的摘要）。报告的数字由 `eval/static_eval.py` 给出。

### 8.1 流程

- 对象：玩过 2 款以上的 64,670 名玩家。按可见游戏数（玩过的款数 − 1）分为 1 / 2-3 / 4-10 / 11+ 共 4 层。
- 键 sha256('static-rec-eval-v1:user:' + player_id) 最小的 1,000 人为验证用户，其余 63,670 人为测试池。验证用户只用于选择配置（8.4）和 C1。
- 测试用户：测试池中层 1 和层 2-3 的全部，层 4-10 键最小的 1,500 人，层 11+ 键最小的 2,500 人。
- 被隐藏的游戏：把用户玩过的游戏按 sha256('static-rec-eval-v1:hide:' + player_id + ':' + appid) 从小到大排列，rep r（r = 0, 1, 2）隐藏第 r + 1 款。rep 数为 min(3, 玩过的款数)，每次只隐藏 1 款。
- v0 用户（与 `graph_v0` 比较）、解释用户（C5）、偏向用户（8.3 的覆盖率等）按键的顺序从测试用户中选取，只用 rep 0。偏向用户把 1,000 人按 W_s 的比例分到各层（最大余数法）。
- 层的权重 W_s 为测试池中层 s 的人数 ÷ 63,670。
- 划分由 `eval/static_split.py` 生成（`eval/results/static_split_v1.json`）。划分中记有 `PLAYED` 的数量和 sha256，图与之不同时，评估不会运行。标签和开发商、发行商的连接（`HAS_TAG`、`DEVELOPED_BY`、`PUBLISHED_BY`）的 sha256 记录在缓存的路径分数和调参结果中，不同时也不会运行。

| 用户 | 1 | 2-3 | 4-10 | 11+ | 合计 | 评估次数 |
|---|---|---|---|---|---|---|
| 对象 | 2,346 | 3,914 | 9,991 | 48,419 | 64,670 | |
| 验证用户 | 44 | 67 | 163 | 726 | 1,000 | 2,956 |
| 测试池 | 2,302 | 3,847 | 9,828 | 47,693 | 63,670 | |
| 测试用户 | 2,302 | 3,847 | 1,500 | 2,500 | 10,149 | 28,145 |
| v0 用户 | 500 | 500 | 500 | 500 | 2,000 | 2,000 |
| 解释用户 | 75 | 75 | 75 | 75 | 300 | 300 |
| 偏向用户 | 36 | 61 | 154 | 749 | 1,000 | 1,000 |

### 8.2 比较的行

| 行 | 内容 |
|---|---|
| `popularity` | 热门度排序（`eval/rankers.py`）。不计被评估用户的记录，相同则按 appid |
| `item_knn` | Item-KNN（`eval/rankers.py`）。与可见游戏的余弦相似度之和，不计被评估用户的记录，用 float32 计算 |
| `graph_v0` | 改进前的方法（`app/api/static_recommend_v0.py`）。只返回前 50 名，第 51 名以后算未命中。只在 v0 用户和偏向用户上运行 |
| `graph_v1_default` | 调参前的配置：λ 为 1 / 1 / 0.5 / 0.1，开发商・发行商除以最大值（旧规则），w_s = ln(1 + 游玩时长)，共玩的种子取前 10 款 |
| `graph_tuned` | 按 8.4 的规则选出、并设为代码默认值的配置 |
| `graph_tuned_minus_peer`、`_tag`、`_studio`、`_pop` | 把 `graph_tuned` 的一个 λ 设为 0（其余不重新调整） |
| `peer_only` | 选定的种子方案，只用共玩（λ_peer = 1，其余为 0） |
| `content_only` | 选定的种子方案，只用标签和开发商・发行商（选定的 λ；两者都为 0 时都取 1） |
| `pop_only` | 只用热门（λ_pop = 1，其余为 0）。用来确认名次与 `popularity` 相同 |

- 图的各行先保存 `collect_path_scores()` 的路径分数，再用 `combine()` 改变 λ 排序。名次是在 9,192 款中去掉可见游戏后的全部游戏（包括无人玩过的游戏）中的名次；`popularity` 和 `item_knn` 是在有人玩过的 8,523 款中的名次。
- 种子方案（`SeedScheme`）共 6 种：w_s 取 ln(1 + 游玩时长)（`log1p`）或 1（`uniform`），共玩的种子取游玩时长最长的前 10 款、前 30 款或全部（`log1p_10` … `uniform_all`）。标签和开发商・发行商始终使用全部种子。
- `graph_v0` 以外的行在测试用户的全部 rep 上排序。与 `graph_v0` 的比较只用 v0 用户的 rep 0，其他行也只取这些次。

### 8.3 评估指标

设被隐藏游戏的名次为 r（不在 `graph_v0` 的前 50 名中时为未命中）。K 取 10、20、50，主要指标为 NDCG@10。HR 和 NDCG 先对每个用户的 rep 取平均，再按 8.4 估计。

| 指标 | 定义 |
|---|---|
| HR@K | r 不超过 K 则为 1，否则为 0 |
| NDCG@K | r 不超过 K 则为 1 / log2(r + 1)，否则为 0 |
| 覆盖率@K | 在偏向用户（n = 1,000，rep 0）的前 K 名中至少出现过 1 次的游戏数 ÷ 8,523（有人玩过的游戏数，不做用户平均）。无人玩过的游戏也计入分子，并另外给出其数量 |
| 多样性@K | 对从前 K 名中任选 2 款的所有组合，求标签集合的距离（1 − Jaccard 系数）并取平均。标签为 `rank` 不超过 10 或没有 `rank` 的 `HAS_TAG` 的 `tag_id`，包括功能（`Feature`），名称相同的类型和用户标签分开计数（与 7.2 的标签路径不同） |
| 新颖性@K | 前 K 名游戏的 −log2(p) 的平均值。p 为玩过该游戏的玩家数 ÷ 全部玩家数（67,894），被评估用户也计入。无人玩过的游戏按 1 人计 |
| 热门前 100 款的占比 | 前 K 名的位置中，玩家数最多的 100 款（相同则按 appid）所占的比例 |

- 覆盖率、多样性、新颖性和热门前 100 款的占比，取 K = 10，在偏向用户上对所有行（包括 `graph_v0`）计算。多样性和新颖性对用户取平均。

### 8.4 统计、配置的选法与判定

- 估计值为事后分层：把各层用户值的平均以 W_s 加权相加。给出全体、1-3（层 1 和层 2-3 以这两层的 W_s 相加）和各层的值。
- 置信区间用在层内重抽用户的 bootstrap 求出（B = 10,000，numpy 的 `default_rng(0)`，百分位法 95% 区间）。每层的 B 次重抽由所有行共用，因此行与行之差是配对的比较。差的双侧 p 值：设 B 个差中 ≤ 0 与 ≥ 0 的个数中较少者为 m，p = min(1, 2(m + 1) / (B + 1))。绝对值不超过 1e-12 的差视为 0（理论上为 0 的差残留的 float 误差）。
- 按被隐藏游戏的游玩时长（不足 1 小时、1 小时以上）以及是否为 Valve 开发或发行的游戏划分的结果，作为描述性结果给出：对每个用户，把落在该组的 rep 的指标之和与次数分别除以 rep 数，以两者事后分层均值之比为估计值。差只看 NDCG@10。

配置的选法（只用验证用户）：

- 候选为 λ_peer = 1，λ_tag ∈ {0, 0.1, 0.25, 0.5, 1}，λ_studio ∈ {0, 0.5, 1, 2}（除以 Σ_s w_s），λ_pop ∈ {0, 0.1, 0.25, 0.5}，与 6 种种子方案组合，共 480 个。`graph_v1_default` 只用来比较，不是候选。
- 目标函数为验证用户上事后分层的 NDCG@10（全体）。
- 目标函数最大的配置记为 best。在比 best 低的幅度不超过该差的 bootstrap 标准误（B = 2,000，`default_rng(0)`）的配置中，按以下顺序取第一个：共玩种子 10 款、30 款、全部 → w_s 为 ln(1 + 游玩时长)、1 → λ_tag、λ_studio、λ_pop 中为 0 的更多 → Σλ 更小 → (λ_tag, λ_studio, λ_pop) 更小。
- 选定的配置在测试之前，与调参结果（`static_tuning_v1.json`）一起作为 `DEFAULT_PATH_WEIGHTS` 和 `DEFAULT_SEED_SCHEME` 提交。只有当调参结果是用同一份计划、划分和图、在验证用户上对全部 480 个配置排序得到的，按此规则从其各行选出的是同一个配置，且选定的配置与代码默认值相同时，测试才会运行。看过测试结果后不再调参。

判定为以下 5 项，其余结果都是描述性的。无论结果如何，报告所有的行、差和判定。

| | 比较的对象 | 成立的条件 |
|---|---|---|
| C1 | 实现检查。在验证用户的全部 rep 上，`peer_only`（`uniform_all`）与 `item_knn`、`pop_only` 与 `popularity` 的名次 | 名次相同或都在第 50 名以后的比例，两者都在 99.9% 以上 |
| C2 | `peer_only` − `item_knn`（全体，NDCG@10） | 95% CI 落在 [−0.01, +0.01] 内。只有这时才称为等价 |
| C3 | `graph_tuned` − `peer_only`（NDCG@10）。主检验为全体，次检验为 1-3 | 对两个 p 值用 Holm 法（两项检验的族错误率 0.05）：p 较小的一个以 0.025 判断，它被拒绝后，另一个以 0.05 判断；被拒绝且差为正则成立。C3 的判定取主检验的结果。同时给出 97.5% CI |
| C4 | `graph_tuned` − `popularity` 和 `graph_tuned` − `item_knn`（全体，NDCG@10） | 95% CI 的下限分别大于 0 和 −0.01 |
| C5 | 在解释用户（rep 0，前 10 名）上，现行推荐中依据的断言全部成立的推荐所占的比例（`eval/explanation_check.py` 写入 `eval/results/static_explanations_v1.json`） | 1.0。v0 的值及对用户 bootstrap 的 CI 仅供参考。该文件生成之前为未定 |

### 8.5 调参结果

在验证用户上调参之后，在这里放入由 `eval/results/static_tuning_v1.json` 生成的表（8.7 的 `report`）。

### 8.6 结果

在测试用户上只运行 1 次之后，在这里放入由 `eval/results/static_test_v1.json` 生成的表。

### 8.7 运行

在仓库根目录下，用安装了 `requirements.txt` 的 Python 运行。使用的图为 UCSD，通过 `.env` 的 `UCSD_NEO4J_*` 连接（在主机上运行时，填写主机能访问的 URI）。`tune` 和 `test` 用 git 确认计划文件已提交，`test` 还确认调参结果和 `app/api/static_recommend.py` 已提交，因此在没有 git 的 `app` 容器中无法运行。

```bash
python -m eval.static_eval collect --stage validation --schemes all --workers 8   # 保存验证用户的路径分数（data/static_eval_cache/，中断后可从断点继续）
python -m eval.static_eval tune --workers 8                                       # 对 480 个配置排序、选定配置并检查 C1 → static_tuning_v1.json
# 提交 static_tuning_v1.json，以及改为选定配置的 app/api/static_recommend.py 的 DEFAULT_PATH_WEIGHTS / DEFAULT_SEED_SCHEME
python -m eval.static_eval collect --stage test --schemes log1p_10,<选定的种子方案> --workers 8
python -m eval.static_eval test --workers 8                                       # 也运行 v0，给出所有的行、差和判定 → static_test_v1.json、static_test_v1_ranks.jsonl.gz
python -m eval.static_eval report --lang zh                                       # 8.5 和 8.6 的表（日语为 --lang ja）
```

- `verify --stage <validation|test|smoke> --schemes <…>` 检查：用 `combine()` 对保存的路径分数排出的前 50 名与 `recommend_games()` 相同，快速的名次计算与 `combine()` 相同。`--stage smoke` 为验证用户中键最小的 30 人，只用于确认能否运行。
- 结果的 JSON 记录 git 的提交、计划和划分的 sha256、图的数量和连接的 sha256、Python 的版本以及各阶段的耗时。`combine()` 和快速名次计算按从前到后的顺序累加 float（Python 3.12 起的 `sum()` 会补偿误差，最后一位可能不同），因此在 Python 3.11 和 3.13 上结果相同。为了固定 Item-KNN 中 float32 求和的顺序，`tune` 和 `test` 会以 `PYTHONHASHSEED=0` 重新启动自身。
- C5 用的 `eval/results/static_explanations_v1.json`，在提交选定的配置之后，用 `python -m eval.explanation_check --split eval/results/static_split_v1.json --k 10 --output eval/results/static_explanations_v1.json` 生成（在解释用户的 rep 0 上核对现行推荐和 v0）。解释用户每层 75 人，因此整体比例相当于各层等权相加。按 W_s 加权的比例（事后分层）及其 CI 也用同一 bootstrap 给出。文件中记录代码的默认值、标签和公司连接的 sha256 以及提交。若这些与测试运行不一致、没有用全部解释用户，或不是用已提交的代码生成的，`test` 把 C5 记为“无法使用”。
- `docker compose run --rm app python -m eval.evaluate --users N --k 10 20 50 --runs R` 保留为快速评估：每次重新选择用户和被隐藏的游戏，重复 `--runs` 次（默认 5），各指标以 平均 ± 标准差 显示（不给出差的置信区间）。静态推荐报告的数字取 `eval/static_eval.py` 的结果。
