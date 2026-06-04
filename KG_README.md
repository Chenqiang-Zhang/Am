# Amazon Reviews'23 All_Beauty 知识图谱

这个项目会基于 `All_Beauty` 类别构建一个可以导入 Neo4j 的第一版商品知识图谱。

## 图谱结构

节点：

- `User(user_id)`
- `Product(product_id, title, main_category, price, average_rating, rating_number)`
- `Review(review_id, title, text, rating, timestamp, helpful_vote, verified_purchase)`
- `Category(category_id, name)`
- `Store(store_id, name)`
- `Feature(feature_id, text, normalized_text)`

关系：

- `(User)-[:WROTE]->(Review)`
- `(Review)-[:REVIEWS]->(Product)`
- `(User)-[:RATED {rating, timestamp, verified_purchase}]->(Product)`
- `(Product)-[:BELONGS_TO]->(Category)`
- `(Product)-[:SOLD_BY]->(Store)`
- `(Product)-[:HAS_FEATURE]->(Feature)`

## 构建 CSV 文件

默认构建方式：使用全部商品元数据，以及前 200,000 条评论。

```bash
python3 scripts/build_kg_csv.py
```

如果想使用全部评论数据：

```bash
python3 scripts/build_kg_csv.py --max-reviews -1
```

输出目录：

```text
kg_output/all_beauty/
```

## 导入 Neo4j

如果你使用的是本地 Neo4j，把生成的文件夹复制到 Neo4j 的 `import` 目录：

```text
$NEO4J_HOME/import/all_beauty/
```

然后在 Neo4j Browser 或 `cypher-shell` 中运行：

```text
neo4j/all_beauty_import.cypher
```

## 直接导入 Neo4j Aura

如果你使用 Neo4j Aura，最省事的方式是通过 Bolt 连接把本地 CSV 批量写入云端数据库。先在本地 `.env` 里设置：

```text
NEO4J_URI=neo4j+s://your-database-id.databases.neo4j.io
NEO4J_USERNAME=neo4j
NEO4J_PASSWORD=your_neo4j_password_here
```

然后运行：

```bash
python3 scripts/import_kg_to_neo4j.py
```

这个方式不需要把 CSV 上传到公开 GitHub 仓库，适合导入当前 `kg_output/all_beauty/` 下的基础图谱。

如果 Aura 当前套餐有 200k 节点限制，建议先构建小型图谱：

```bash
python3 scripts/build_kg_csv.py \
  --max-reviews 30000 \
  --max-meta 20000 \
  --max-features-per-product 10 \
  --output-dir kg_output/all_beauty_aura_small

python3 scripts/import_kg_to_neo4j.py --input-dir kg_output/all_beauty_aura_small
```

## 使用 Neo4j Aura Import UI 导入

如果你使用的是 Neo4j Aura 网页版 Import 工具，有两种导入方式：

1. 使用 Import UI 上传 CSV 并手动建模。
2. 使用 Cypher `LOAD CSV`，但 CSV 必须先上传到公网可访问的 HTTPS 地址。

Aura 不能通过 `file:///` 读取你的本地文件系统。如果要在 Aura 里用 `LOAD CSV`，需要先把 CSV 放到公开 HTTPS 地址，然后使用 `neo4j/all_beauty_import_github_chunks.cypher` 这类 HTTPS 版本脚本。

如果你没有公网 HTTPS CSV 地址，建议使用 Import UI。你需要在 Import UI 中上传或选择 CSV 文件，然后手动定义下面的映射关系。

## 使用 GitHub Raw URL 导入 Aura

GitHub 网页上传单个文件时通常会遇到 25MB 左右的限制。因此本项目提供了切分脚本，把大 CSV 切成小于 25MB 的分片：

```bash
python3 scripts/split_csv_for_aura_github.py
```

切分后的文件在：

```text
kg_output/all_beauty_github/all_beauty/
```

目前大文件会被切成：

```text
nodes_features_part001.csv
nodes_features_part002.csv
nodes_reviews_part001.csv
nodes_reviews_part002.csv
nodes_reviews_part003.csv
```

其他较小的 CSV 会原样复制过去。

把 `kg_output/all_beauty_github/all_beauty/` 这个文件夹上传到一个公开 GitHub 仓库。建议单独建一个数据发布仓库，代码仓库里只保存脚本和文档。假设你的数据仓库地址是：

```text
https://github.com/USER/REPO
```

并且文件在仓库的：

```text
all_beauty/nodes_users.csv
```

那么 raw base URL 就是：

```text
https://raw.githubusercontent.com/USER/REPO/main
```

然后打开：

```text
neo4j/all_beauty_import_github_chunks.cypher
```

把里面所有：

```text
https://raw.githubusercontent.com/USER/REPO/main
```

替换成你的真实 raw base URL，再复制到 Neo4j Aura 的 Query 页面运行。

节点映射：

| CSV | 节点标签 | ID 字段 | 属性 |
|---|---|---|---|
| `nodes_users.csv` | `User` | `user_id` | `user_id` |
| `nodes_products.csv` | `Product` | `product_id` | `product_id`, `title`, `main_category`, `price`, `average_rating`, `rating_number` |
| `nodes_reviews.csv` | `Review` | `review_id` | `review_id`, `title`, `text`, `rating`, `timestamp`, `helpful_vote`, `verified_purchase` |
| `nodes_categories.csv` | `Category` | `category_id` | `category_id`, `name` |
| `nodes_stores.csv` | `Store` | `store_id` | `store_id`, `name` |
| `nodes_features.csv` | `Feature` | `feature_id` | `feature_id`, `text`, `normalized_text` |

关系映射：

| CSV | 关系类型 | 起点节点 | 起点字段 | 终点节点 | 终点字段 | 属性 |
|---|---|---|---|---|---|---|
| `rel_wrote.csv` | `WROTE` | `User` | `user_id` | `Review` | `review_id` | 无 |
| `rel_reviews.csv` | `REVIEWS` | `Review` | `review_id` | `Product` | `product_id` | 无 |
| `rel_rated.csv` | `RATED` | `User` | `user_id` | `Product` | `product_id` | `rating`, `timestamp`, `verified_purchase` |
| `rel_product_category.csv` | `BELONGS_TO` | `Product` | `product_id` | `Category` | `category_id` | 无 |
| `rel_product_store.csv` | `SOLD_BY` | `Product` | `product_id` | `Store` | `store_id` | 无 |
| `rel_product_feature.csv` | `HAS_FEATURE` | `Product` | `product_id` | `Feature` | `feature_id` | 无 |

完成映射后运行导入。导入成功后，打开 Query 或 Explore，运行下面的检查和推荐查询。

## 推荐查询示例

推荐与用户高评分商品共享特征的商品：

```cypher
MATCH (u:User {user_id: $user_id})-[r:RATED]->(p:Product)-[:HAS_FEATURE]->(f:Feature)<-[:HAS_FEATURE]-(rec:Product)
WHERE r.rating >= 4 AND rec <> p
RETURN rec.product_id AS product_id,
       rec.title AS title,
       count(DISTINCT f) AS shared_features,
       collect(DISTINCT f.text)[0..5] AS evidence_features
ORDER BY shared_features DESC, rec.average_rating DESC
LIMIT 20;
```

当用户的长文本需求已经被映射成特征词后，推荐匹配这些特征的商品：

```cypher
MATCH (f:Feature)<-[:HAS_FEATURE]-(p:Product)
WHERE f.normalized_text IN $feature_texts
RETURN p.product_id AS product_id,
       p.title AS title,
       p.average_rating AS average_rating,
       count(DISTINCT f) AS matched_features,
       collect(DISTINCT f.text) AS evidence_features
ORDER BY matched_features DESC, average_rating DESC
LIMIT 20;
```

查询某个推荐结果的解释路径：

```cypher
MATCH path = (u:User {user_id: $user_id})-[:RATED]->(:Product)-[:HAS_FEATURE]->(:Feature)<-[:HAS_FEATURE]-(rec:Product {product_id: $product_id})
RETURN path
LIMIT 5;
```

## 下一步如何接入 LLM

图谱构建完成后，可以把 LLM 用在三个位置：

- 将用户输入的长文本需求解析成结构化约束和特征词。
- 从商品描述和评论文本中抽取更丰富的 feature/aspect 节点。
- 将 Neo4j 查询到的路径转换成自然语言推荐理由。

最重要的设计原则是：LLM 应该解释从图谱中检索到的证据，而不是编造证据。

## 使用 OpenAI API 抽取商品属性

先从商品元数据中抽取商品级属性，输出 JSONL，确认质量后再并入图谱：

```bash
cp .env.example .env
# Edit .env and set OPENAI_API_KEY or DEEPSEEK_API_KEY.
python3 scripts/extract_product_attributes_llm.py \
  --provider deepseek \
  --limit 20 \
  --resume \
  --compact-input \
  --skip-sparse \
  --batch-size 5
```

默认输出：

```text
kg_output/attributes/product_attributes_llm.jsonl
```

建议流程：

1. 先运行 `--limit 20`，人工检查属性质量。
2. 如果质量稳定，再运行更大的批次，例如 `--limit 1000 --resume`。
3. 后续把输出转换成 `Attribute` 或增强版 `Feature` 节点，并建立 `(Product)-[:HAS_ATTRIBUTE]->(Attribute)` 关系。

转换成 Neo4j CSV：

```bash
python3 scripts/attributes_to_kg_csv.py
```

这会在 `kg_output/all_beauty/` 下生成：

```text
nodes_attributes.csv
rel_product_attribute.csv
```

导入基础图谱后，再运行：

```text
neo4j/import_openai_attributes.cypher
```

如果使用 Neo4j Aura，可以直接通过 Bolt 导入属性 CSV：

```bash
python3 scripts/import_attributes_to_neo4j.py --input-dir kg_output/all_beauty_aura_small
```
