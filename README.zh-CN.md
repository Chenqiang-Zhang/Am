# Amazon Reviews'23 知识图谱推荐系统

[English](README.md) | [日本語](README.ja.md) | **中文**

一个基于 Amazon Reviews'23 数据集构建的实验性商品推荐系统。类型/品类可通过 `config.yaml` 配置（当前为 `Video_Games`，下文所有默认值/示例均基于该类型）——schema、pipeline 脚本和 Cypher 查询都与具体类型无关（genre-agnostic）。系统将评论数据和商品元数据转换为 Neo4j 知识图谱，然后暴露一个 REST API，由 LLM 直接针对该图谱编写并执行 Cypher 查询——因此每一条推荐理由都是真实、可检查的图路径，而不是黑箱。

## 架构

```
原始数据 (Amazon Reviews'23)
    ↓ select_kcore.py（决定 k-core 用户/商品筛选范围——数据规模）
    ↓ build_base_graph.py
基础图 CSV（Product/User/Review/Category/Brand）
    ↓ extract_product_attributes.py（基于规则的 `details` 提取 + LLM 文本提取，两者合并）
    ↓ extract_review_mentions.py（LLM，从评论文本中提取）
    ↓ canonicalize_attributes.py（可选——LLM 驱动的 attr_type/value 同义词归一化）
    ↓ build_attribute_graph.py
属性节点/边 CSV（与类型无关的 attr_type）
    ↓ import_kg_to_neo4j.py（一次性导入以上所有内容）
Neo4j 知识图谱
    ↓ backfill_display_fields.py --images --titles-ja（可选——补充 Product.image_url / Product.title_ja）
    ↓
REST API（FastAPI）
    ├── Text2Cypher 搜索         （LLM 针对每个请求编写并执行一条 Cypher 查询；图 schema 和
    │                            attr_type 词表都从当前图谱实时读取，因此 prompt 会自适应
    │                            实际加载的类型/目录——没有硬编码的品类；一旦生成/执行失败
    │                            或查询合法地返回零行，则回退到热门商品查询）
    ├── 个性化                   （用户评分/属性历史 + 过去成功的查询作为 few-shot 示例；
    │                            只有当某个 user_id 拥有真实的 RATED/属性历史时，$uid 才会被绑定）
    ├── 首页推荐                 （基于行为，没有查询文本；对于没有历史记录的用户完全跳过 LLM，
    │                            直接回退到热门商品查询、不产生任何额外延迟——并将每个
    │                            已个性化用户生成的查询缓存在内存中，以便标签页被隐藏/关闭时
    │                            触发的后台"预热"调用能让下次打开页面时瞬间完成）
    ├── 对话式聊天               （LLM 基于同一份实时 attr_type 词表，自行决定该问什么、何时开始搜索
    │                            ——与类型无关；Python 仅强制限制提问次数上限，并在 LLM 调用本身
    │                            失败时提供兜底方案）
    └── 评论查询与浏览日志记录（全部写入 Neo4j）
```

## 图谱 Schema

权威的 schema 定义位于 [`Graph_rule.md`](Graph_rule.md)（与仓库根目录下的副本 [`../Graph_rule.md`](../Graph_rule.md) 保持同步）。概要如下：

**节点（Nodes）**

| 标签 | 关键属性 |
|---|---|
| `User` | `user_id` |
| `Product` | `product_id`、`title`、`title_ja`、`price`、`avg_rating`、`rating_count`、`description`、`image_url` |
| `Review` | `review_id`、`title`、`title_ja`、`text`、`text_ja`、`rating`、`timestamp`、`helpful_vote`、`verified` |
| `Category` | `category_id`、`name`、`level` |
| `Brand` | `brand_id`、`name` |
| `Attribute` | `attribute_id`、`attr_type`、`value`、`value_ja`（LLM 提取，与类型无关） |
| `SearchLog` | `log_id`、`query`、`cypher`、`explanation`、`result_product_ids`、`result_count`、`timestamp`（个性化 few-shot 用） |

**关系（Relationships）**

| 关系 | 方向 | 属性 |
|---|---|---|
| `WROTE` | User → Review | — |
| `ABOUT` | Review → Product | — |
| `RATED` | User → Product | `rating`、`timestamp` |
| `VIEWED` | User → Product | `timestamp`、`search_id` |
| `SEARCHED` | User → SearchLog | — |
| `BELONGS_TO` | Product → Category | — |
| `SUBCATEGORY_OF` | Category → Category | — |
| `MADE_BY` | Product → Brand | — |
| `HAS_ATTRIBUTE` | Product → Attribute | — |
| `MENTIONS` | Review → Attribute | `sentiment`、`confidence` |

早期版本中的 `Store`/`Feature` 节点以及 `HAS_FEATURE`/`REVIEWS` 关系已被弃用——特性文本已并入 `Product.description`，`Store` 已合并进 `Brand`。

## 仓库结构

```text
.
├── README.md
├── Graph_rule.md          # schema 文档的完整副本，人工与 ../Graph_rule.md 保持同步
├── config.yaml            # 类型/数据规模/LLM 提供商/API 配置
├── requirements.txt
├── data/                  # 原始数据——仅本地存在，不纳入版本控制
├── kg_output/             # 生成的 CSV——仅本地存在，不纳入版本控制
├── docs/                  # 本地文档——不纳入版本控制
├── kg_build/              # KG 构建 pipeline——数据生成（运行方式：python kg_build/<name>.py）
│   ├── select_kcore.py            # 1. 决定 k-core 用户/商品筛选范围（数据规模）
│   ├── build_base_graph.py        # 2. 构建基础图 CSV（Product/User/Review/Category/Brand）
│   ├── extract_product_attributes.py  # 3a. 属性提取：零成本的基于规则提取（元数据 `details`，与类型无关）+ LLM（title/features/description），两者合并
│   ├── extract_review_mentions.py     # 3b. 从评论文本中用 LLM 提取属性提及
│   ├── canonicalize_attributes.py     # 4.（可选）LLM 驱动的 attr_type/value 同义词归一化
│   ├── build_attribute_graph.py       # 5. 合并上述提取结果 → 属性节点/边 CSV
│   ├── import_kg_to_neo4j.py          # 6. 通过 Bolt 导入所有内容（本地 Neo4j 或 Aura）
│   ├── backfill_display_fields.py     # 7.（可选）为现有节点补充 Product.image_url / Product.title_ja / Review.title_ja+text_ja / Attribute.value_ja
│   ├── wipe_neo4j.py                  # （工具）清空当前配置的 Neo4j 实例中的所有数据
│   └── utils/                         # 仅在 kg_build/ 内部使用的共享辅助模块——不直接运行
│       ├── llm_client.py              #   LLM 客户端构建器（gemini/groq/deepseek/openai/ollama）
│       ├── llm_json.py                #   Chat/responses JSON 调用 + 带回退的批处理辅助函数
│       ├── neo4j_io.py                #   .env 加载 + Neo4j 连接解析
│       ├── csv_io.py                  #   JSONL/CSV 读写辅助函数
│       └── text_utils.py              #   文本清洗/attr_type 归一化/ID 哈希辅助函数
├── eval/                  # 评估——读取实时图谱 + app/api/，独立于 kg_build/
│   └── eval_offline.py            # 离线留一法评估（HR@K/NDCG@K，与 Item-KNN 基线对比）
├── app/                   # 正在运行的应用（后端 + 前端）
│   ├── api/                       # 推荐 REST API
│   │   ├── main.py                # FastAPI 应用/路由
│   │   ├── recommender.py         # Text2Cypher 生成、聊天、个性化、评论
│   │   └── models.py              # Pydantic 请求/响应模型
│   └── web/                       # React + TypeScript 对话式 UI
```

## 快速开始

### 1. 安装依赖

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. 配置环境

```bash
cp .env.example .env
```

编辑 `.env`，填写：
- `NEO4J_URI` / `NEO4J_PASSWORD`（Neo4j Aura 连接信息）
- 你在 `config.yaml`（`llm.provider`）中选择的 LLM 提供商所对应的 API 密钥：`GEMINI_API_KEY`、`GROQ_API_KEY`、`DEEPSEEK_API_KEY` 或 `OPENAI_API_KEY`。Gemini 和 Groq 都提供免费额度。

`config.yaml` 控制 LLM 提供商/模型、数据路径以及 Text2Cypher 的重试设置——详见该文件中的注释。商品/用户的筛选范围由 k-core 的大小单独控制（`select_kcore.py` 的 `--k`，见下方第 4 步），而不是通过配置项控制。

### 3. 启动 Neo4j

**方案 A —— Docker（本地开发）：**

```bash
docker run -d --name neo4j-am \
  -p 7474:7474 -p 7687:7687 \
  -e NEO4J_AUTH=neo4j/password123 \
  neo4j:latest
```

然后在 `.env` 中设置：
```
NEO4J_URI=bolt://localhost:7687
NEO4J_USERNAME=neo4j
NEO4J_PASSWORD=password123
NEO4J_DATABASE=neo4j
```

**方案 B —— Neo4j Aura（云端）：** 在 `.env` 中设置 `NEO4J_URI=neo4j+s://...`。免费版 Aura 实例在空闲一段时间后会自动暂停——运行 API 或 pipeline 脚本前，请先到 [Aura console](https://console.neo4j.io) 恢复实例。

### 4. 构建并导入知识图谱

将 Amazon Reviews'23 的数据文件放到本地（路径来自 `config.yaml` 的 `data` 部分；当前为 `Video_Games` 品类）：
```text
data/Video_Games.jsonl.gz
data/meta_Video_Games.jsonl.gz
```

可直接从数据集的原始文件托管地址下载（`Video_Games` 合计约 1 GB；将两个 URL 中的品类名替换即可切换到其他类型）：
```bash
wget -P data https://mcauleylab.ucsd.edu/public_datasets/data/amazon_2023/raw/review_categories/Video_Games.jsonl.gz
wget -P data https://mcauleylab.ucsd.edu/public_datasets/data/amazon_2023/raw/meta_categories/meta_Video_Games.jsonl.gz
```

按顺序运行 pipeline（均位于 `kg_build/` 下；与 `import_kg_to_neo4j.py` 中的 docstring 一致）：

```bash
# 1. 决定纳入哪些用户/商品：一个二分 k-core，其中每个用户和每个商品
#    至少有 k 次交互（默认 k=14——针对 Video_Games 校准；其他 k 值
#    会产生什么结果见下方"数据规模"一节，切换类型时请重新扫描）。默认情况下，
#    缺少 price/avg_rating/rating_count/description/brand/category 中任一项的商品
#    会在计算 k-core 之前就被排除（传入 `--allow-incomplete-metadata` 可保留它们）。
#    结果会写入 <output_dir>/kcore_selection 下的
#    selected_user_ids.txt / selected_product_ids.txt。
python3 kg_build/select_kcore.py --k 14

# 2. 构建基础图（Product/User/Review/Category/Brand），范围限定为
#    第 1 步选出的 k-core 集合。计算时会读取完整的评论文件，因此这是
#    最慢的一步（对于 Video_Games 大约需要几分钟）。
python3 kg_build/build_base_graph.py

# 3. 属性提取（商品元数据 + 评论提及）。
#    --product-ids-file 将提取范围限定为第 2 步实际选入基础图的商品——
#    只要涉及真实的 LLM 调用就务必带上此参数，否则提取会遍历整个原始
#    目录，而不仅是约 1.5k 个 k-core 商品。
python3 kg_build/extract_product_attributes.py --resume --product-ids-file kg_output/video_games/nodes_products.csv

#    评论提及的规模随评论数量变化（默认 k=14 核约为 56k 条），而非
#    商品数量——请将其视为在样本上尽力而为的补充，而非要跑完的任务。
#    可用 --resume 在之后扩大样本范围：
python3 kg_build/extract_review_mentions.py --resume --limit 5000 --min-text-len 60 --batch-size 15

# 4.（可选）归一化上述两个独立提取流程产生的 attr_type/value 同义词
#    （例如 "item_form" 与 "texture"）
python3 kg_build/canonicalize_attributes.py

# 5. 将提取结果合并为属性节点/边 CSV
#    （应用第 4 步的归一化映射，并丢弃 nodes_products.csv 之外的商品的
#    属性，如果存在的话）
python3 kg_build/build_attribute_graph.py

# 6. 通过 Bolt 导入所有内容（基础图 + 属性，如果 CSV 存在的话）
python3 kg_build/import_kg_to_neo4j.py

# 7.（可选）从元数据补充 Product.image_url（使 UI 中能显示商品缩略图）、
#    将 Product.title 翻译为日语（Product.title_ja）、将 Review.title/text
#    翻译为日语（Review.title_ja/text_ja，供 lang=ja 时的 GET /products/{id}/reviews 使用），
#    以及/或者将 Attribute.value 翻译为日语（Attribute.value_ja——Text2Cypher 的 few-shot
#    示例总是会连同 value 一起采集它，因此一旦该翻译存在，matched_attrs 就会显示出来）。
#    --reviews-ja 会按 helpful_vote 降序优先处理（即最有可能实际展示出来的评论）——
#    如果 LLM 预算有限，可搭配 --reviews-limit/--values-limit 使用。扩大规模后重新运行
#    是安全的——只会处理尚未翻译的商品/评论/属性值。
python3 kg_build/backfill_display_fields.py --images --titles-ja --reviews-ja --reviews-limit 2000 --values-ja
```

k 越大，图越稠密（每个用户的平均交互数越多），但用户/商品数量越少。如需重新选择，需从第 1 步重新执行（因为选择结果会变化，第 2 步及之后也必须重新运行）。

第 3 步的 `--provider`/`--model` 默认取自 `config.yaml` 的 `llm.provider`/`llm.model`；显式传入这两个参数即可覆盖默认值。

`extract_product_attributes.py` 总是先对元数据的 `details` 字段执行一遍零成本、与类型无关的
基于规则的提取（确定性的，不调用 LLM，也不需要手工维护键映射），然后再让 LLM
处理 title/features/description。每个商品由规则得出的属性会作为 `known_attributes`
传给 LLM，使其只提取真正新增的信息，而不是重复推导相同的事实。要完全跳过 LLM、
以零成本为每个商品提取属性：
```bash
python3 kg_build/extract_product_attributes.py --rule-only --limit -1
```
当出于预算原因，只想在一个较小的 `--limit` 上运行需要付费的 LLM 步骤，同时仍希望用
基于规则的属性覆盖整个目录时，这一点很有用。

### 5. 运行离线评估

导入完成后、在接触 API 之前运行是可选但推荐的做法——它只需要一个可用的实时 Neo4j
连接（通过 `app/api/recommender.py` 的 `Recommender`），不需要启动 API 服务：

```bash
# 先在小样本上快速做健全性检查
python3 eval/eval_offline.py --cutoffs 10 20 50 --sample 100

# 对每个符合条件的用户做完整的留一法评估
python3 eval/eval_offline.py --cutoffs 10 20 50 --resume
```

运行前会先打印数据健全性预检（商品/用户/评论数量、价格/图片/评分覆盖率），然后将基于
RATED 的个性化（`recommend_home`，通过 Text2Cypher）与两个基线比较——Item-KNN（用户-物品
评分矩阵上的余弦相似度）和 Popularity（按 rating_count/avg_rating 的静态排名）。**cutoff**
即 HR@K/NDCG@K 中的 K——衡量"我们有没有找到它"时看多少条 top 推荐结果：cutoff 为 10
问的是被留出的商品是否落在 top 10 内，cutoff 为 50 则给了这些方法宽裕得多的空间。
`--cutoffs`（默认 10/20/50）会同时计算这里列出的每个截断值下的留一法 HR@K/NDCG@K
（只按最大的截断值取一次结果，再逐级截断，而非重复请求）。每个符合条件的用户，其最近
一条 ≥4 星的评分被作为目标留出，该边（以及之后评分的任何内容）被临时移除，三种方法
都会尝试将被留出的商品重新排进 top K 中。除了 HR@K/NDCG@K 之外，汇总结果中还会报告
各方法的 `catalog_coverage@k`/`avg_rating@k`（取最小截断值下的数值）——这些本身就是
独立的指标，用于揭示某个方法是否只是反复推荐同一批热门商品，而不只是看它的命中率。
结果会写入 `eval_results.jsonl` / `eval_results.summary.json`（路径可通过 `--out` 配置）。

### 6. 启动推荐 API

```bash
uvicorn app.api.main:app --reload
```

或者，若想使用 `config.yaml` 中的 `api.host`/`api.port` 而非 uvicorn 的默认值：
```bash
python -m app.api.main
```

打开 `http://localhost:8000/docs`（或你配置的 host/port）即可访问交互式 Swagger UI。

### 7. 启动前端 UI

```bash
cd app/web
npm install
npm run dev
```

打开 `http://localhost:5173`。Vite 开发服务器会将 `/api/*` 代理到 `http://localhost:8000`。

### 8. 试用一下

1. 打开页面后，聊天页面会在你输入任何内容之前立即显示第一批推荐结果——这并不是一次
   聊天轮次（不会出现"assistant is typing"气泡），只是一次轻量级的后台
   `POST /recommend/home` 请求。系统始终有一个已选定的测试用户（见下文）；没有评分历史的
   用户会得到即时的热门商品兜底结果（不调用 LLM），有历史记录的用户会得到个性化结果，
   且再次访问时会更快——参见下方关于缓存的说明。
2. 在聊天 UI 顶部，从下拉框（`TestUserSelect`）中选择一个**测试用户**——可以是内置的
   "オリジナルテストユーザー" 演示 ID（无个性化；这是默认选项），也可以是从
   `GET /users/sample` 实时获取的某个真实 `user_id`（这些是在当前图谱中拥有 ≥3
   条评分的真实用户，因此选中他们会真正显示个性化的结果/首页推荐）。
3. 用自然语言输入一个查询（日语或英语），例如 "小学生の子供と一緒に遊べる協力プレイのSwitchゲームが欲しい"
   或 "a co-op couch game for the PS5 that's fun for kids and adults together"。
4. 助手会要么提出一个澄清性问题（回答它，或选择 "こだわらない" / "no
   preference" 来跳过），要么在已有足够信号时直接开始搜索。
5. 推荐结果会展示 LLM 生成的一句话 `explanation`——以 UI 当前语言书写（在页面顶部
   切换 "日本語"/"EN"）——并且在开发模式下还会显示匹配到的属性以及原始生成的
   Cypher（`intent.cypher`），使每一条推荐理由都可检查，而不是黑箱。如果 Text2Cypher
   的生成/执行失败，或者查询合法地没有匹配到任何内容，列表会回退显示热门高评分商品，
   而不是展示一个空白页面（响应中的 `fallback: true`）。
6. 打开 "レビューを見る" 或点击 "Amazon.comで見る" 会通过 `/behavior/view` 记录一条
   `VIEWED` 边，并关联到发起该操作的 `search_id`。`_get_dynamic_few_shot()` 会读取这些
   数据（与 `SearchLog` 联合查询），在为该用户构建下一次 prompt 时优先采用那些
   曾导致点击的历史查询。
7. 一旦某个有历史记录的测试用户已经加载过首页推荐，切换标签页或关闭标签页会触发一个
   beacon 请求到 `POST /recommend/home/warm`，在后台重新生成并缓存该用户的个性化查询。
   下次打开页面时（在 1 小时缓存有效期内），`/recommend/home` 会直接从该缓存中即时返回结果，
   而不必等待 LLM。
8. 切换 **General mode（通用模式）**会以不带 `user_id` 的方式发送当前请求（等同于一个
   全新的匿名用户所走的代码路径，即热门商品兜底），且不会改变当前选中的测试用户——
   这样就能在同一个用户身份下，随时切换查看"为我个性化的结果"和"陌生人会看到的结果"。
   **Clear history（清除历史）**会通过 `POST /users/{user_id}/clear_history` 删除当前选中
   用户的 `VIEWED`/`SearchLog` 历史——`RATED`（数据集自带的评分历史，是个性化的依据）
   永远不会被触碰。

如果 pipeline 中的第 4 步（Neo4j 导入）尚未运行，或者 Neo4j 不可达，`/health` 仍会
返回 `ok`，但 `/recommend`/`/chat` 调用会失败——请先查看 API 进程的 stderr 输出。

## API 用法

### `POST /recommend`

接收一个自然语言查询。LLM 会针对图谱生成一条 Cypher 查询并返回其结果。个性化只有在
`user_id` 指向一个拥有真实 `RATED`/属性历史的用户时才会生效——没有历史记录的
`user_id` 会被当作匿名请求处理（不绑定 `$uid`，因此 LLM 无法引用它；有一个校验器
会拒绝任何引用了未绑定的 `$uid`、或者用字面量 `user_id` 字符串硬编码而非使用 `$uid`
的生成 Cypher）。

**请求：**
```json
{
  "query": "I have dry and sensitive skin, looking for a gentle face moisturizer with hyaluronic acid, preferably fragrance-free",
  "user_id": null,
  "limit": 10,
  "lang": "en"
}
```

**响应（节选）：**
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

`lang`（"ja" | "en"，默认为 "en"）控制顶层 `intent.cypher_explanation` 以及每条推荐的
`explanation` 所使用的语言——LLM 被指示用请求指定的语言书写两者（prompt 中的 few-shot
示例仅用英语作为示范），并且——如果已经运行过 `backfill_display_fields.py --titles-ja`——
`lang="ja"` 还会用缓存的日语翻译填充 `display_title`（否则为 `null`；前端会回退使用
`title`）。

如果 Cypher 的生成/执行在重试后仍然失败，**或者生成的查询成功执行但返回零行**，
响应会回退到一个基于热门度的查询（`fallback: true`），而不是展示空结果。

### `POST /recommend/home`

不带查询文本的基于行为的推荐（需要 `user_id`，`lang` 为可选参数，同上）。对于没有
`RATED`/属性历史的用户，这会完全跳过 LLM，直接从 Neo4j 返回热门高评分商品
（不调用 LLM，除数据库往返外几乎没有额外延迟）——这正是用户尚未输入任何内容之前
展示的初始推荐所走的路径，因为内置测试用户一开始就没有历史记录。对于有历史记录的
用户，LLM 会在首次调用时生成一条个性化的 Cypher 查询，其结果会在服务端缓存
（按 `user_id`+`lang`+`limit`，1 小时有效期）——之后的调用会直接从该缓存中即时返回。
关于该缓存如何在用户提问之前就被预先填充，见下方的 `/recommend/home/warm`。

### `POST /recommend/home/warm`

即发即弃（fire-and-forget）：接收与 `/recommend/home` 相同的请求体，并总是立即返回
`204`。它会在后台触发与 `/recommend/home` 相同的缓存填充生成过程，但不等待其完成，
也不写入 `SearchLog` 条目（因此反复切换标签页不会刷屏产生搜索历史）。Web UI 会在
`visibilitychange`/`pagehide` 事件中通过 `navigator.sendBeacon` 调用它，因此用户的
个性化首页推荐通常在他们重新打开应用之前就已经缓存好了。

### `POST /behavior/view`

记录某用户浏览了某商品（`user_id`、`product_id`、可选的 `search_id`），作为一条
`VIEWED` 边，用作个性化信号。当测试用户在推荐卡片上点击 "Amazon.comで見る" 时，
Web UI 会调用此接口。

### `POST /chat`

运行一轮对话式推荐。每一轮，LLM 都会被提供图谱中实际存在的属性类型
（从 Neo4j 查询一次并缓存）以及来自 `config.yaml` 的类型信息，并通过其结构化响应中的
`action`/`filled_slots` 自行决定是要再问一个澄清性问题，还是转入搜索；这使得提问流程
能够适应当前加载的任何目录/类型，没有硬编码的品类或问题模板。Python 只强制
限制一个提问次数上限（`MAX_QUESTIONS = 5`），并在 LLM 调用本身失败时立即回退到搜索。
一旦触发搜索，就会委托给与 `/recommend` 相同的 Text2Cypher 路径。

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

响应有以下两种情况：
- `action: "ask"`，附带一个问题和快速回复选项（`search_id: null`）
- `action: "search"`，附带 `preference_summary`、`intent`（cypher/explanation）、推荐结果，
  以及 `search_id`（便于之后的 `/behavior/view` 调用关联回这次搜索）

### `GET /users/sample`

返回少量拥有评分历史的真实 `user_id`，用于在没有身份验证系统的情况下演示个性化功能。

### `GET /products/{product_id}/reviews`

返回某商品的热门评论，按有用票数排序。`lang`（"ja" | "en"，默认为 "en"）在存在
`Review.title_ja`/`text_ja`（来自 `backfill_display_fields.py --reviews-ja`）时优先
使用它们，否则回退到原始文本。

### `POST /users/{user_id}/clear_history`

删除该用户的 `VIEWED` 和 `SearchLog`/`SEARCHED` 历史（以及由此生成的任何缓存的首页
推荐结果）。`RATED`——数据集自带的评分历史，也是个性化真正的依据——永远不会被触碰；
此接口只是重置演示过程中累积的行为/搜索日志。

## 数据规模

数据规模由 `select_kcore.py` 的两个机制共同控制：一是元数据完整性过滤（默认开启——
先排除缺少 `price`/`avg_rating`/`rating_count`/`description`/`brand`/`category` 中任一项的
商品；对 `Video_Games` 而言，这会排除原始评分边中约 31% 的部分，几乎全部由缺少 `price`
导致；可通过 `--allow-incomplete-metadata` 关闭），二是 k-core 的大小（`--k`，默认为 14）
——在剩余数据中，找出每个用户和每个商品都至少有 k 次交互的最大二分子图。两者共同保证了
图谱中的每个用户/商品既有完整的元数据，又有足够的历史记录使个性化和离线评估变得有意义
（不同于简单的 top-N 商品采样，后者会使大多数用户只有一条评分）。
`<output_dir>/kcore_selection/kcore_summary.json` 记录了给定 k 值下最终的用户/商品/边数量；
运行 `build_base_graph.py` 之后，实际导入的数量会写入 `kg_output/<output_dir>/build_summary.json`。

提高 k 会使图谱迅速缩小（这是一个迭代式的二分剥离过程，而非线性截断），但会使其
更密——每个存活下来的用户/商品拥有更多交互，这对个性化和离线评估的质量都很重要。
针对 `Video_Games` 的实测数据（已应用默认的元数据完整性过滤）：

| k | 用户数 | 商品数 | 边数 | 人均边数 | 商品均边数 |
|---|---|---|---|---|---|
| 8 | 14,428 | 5,815 | 195,254 | 13.53 | 33.58 |
| 10 | 6,724 | 3,369 | 112,380 | 16.71 | 33.36 |
| 12 | 2,917 | 1,728 | 57,354 | 19.66 | 33.19 |
| **14（默认）** | **861** | **610** | **18,771** | **21.80** | **30.77** |
| 15+ | — | — | — | — | 核心为空（k=15 时坍缩） |

14 是课程项目规模下实际的最佳平衡点：足够密集以支撑有意义的图路径和离线评估，
又足够小，使基础图构建、LLM 属性提取和 Neo4j 导入都能在笔记本电脑上于合理时间内
完成。需要说明的是，这张表的数值比引入元数据完整性过滤之前 `select_kcore.py` 给出的
数值要小——同样的 k 下，由于元数据不完整的商品（以及仅依赖它们的交互）在计算
k-core 之前就被排除了，用户/商品数因此减少。

## 后续步骤

- 将 LLM 属性提取的覆盖范围扩大到完整商品集
- 增加一个利用用户间共享评分历史的协同过滤 few-shot 路径
- 增加一个多步图探索接口（`GET /product/{id}/related`）
- 曾尝试并移除了对推荐理由的显式反馈（`GAVE_FEEDBACK` 边/点赞点踩 UI）
  ——`_get_dynamic_few_shot()` 的隐式点击信号（`VIEWED` 与 `SearchLog` 联合查询）
  已经覆盖了同样的"这次搜索是否有用"的需求，无需额外的 UI/接口
- 首页推荐缓存（`Recommender._home_cache`）存在于进程内存中——重启后会重置，
  如果 API 未来扩展为多实例部署也不会共享；如果这成为问题，可将其迁移到 Neo4j
  或共享存储（例如 Redis）
- 免费版 LLM 提供商（尤其是 Groq）每日 token 配额较低，课堂演示很容易很快耗尽
  ——如果 `/recommend`/`/chat` 开始意外返回 `fallback: true`，可以将 `config.yaml` 的
  `llm.provider` 切换为配额更高的 `gemini` 作为备选方案（可查看 API 进程的 stderr
  中是否有 `429`/`rate_limit_exceeded` 错误来确认）
- 合并前对照了并行推进的 `main` 分支评估/UI 相关工作。以下内容是有意未移植的
  （需要先做设计工作，而非机械式移植）：
  - `main` 中更细粒度的行为事件分类（impression/filter_change/restart 等，比我们单一的
    `VIEWED` 边更丰富）以及匿名浏览器 ID 身份模型——后者与本分支移除匿名测试用户选项的
    决定直接冲突
  - 覆盖缺口分析（coverage-gap analysis）与 discoverable-pool 范围评估——`main` 的实现
    强依赖我们schema中没有的商品质量/可售状态字段
