# Amazon Reviews'23 All_Beauty 知识图谱

基于 Amazon Reviews'23 `All_Beauty` 类别构建的 Neo4j 知识图谱，用于支持带推荐根据的商品推荐系统。

## 图谱结构

### 节点

| 标签 | 属性 | 说明 |
|---|---|---|
| `User` | `user_id` | 评论用户 |
| `Product` | `product_id`, `title`, `main_category`, `price`, `average_rating`, `rating_number` | 商品 |
| `Review` | `review_id`, `title`, `text`, `rating`, `timestamp`, `helpful_vote`, `verified_purchase` | 评论 |
| `Category` | `category_id`, `name` | 商品类别 |
| `Store` | `store_id`, `name` | 出品方/品牌（以 `store` 字段代替 `brand`） |
| `Feature` | `feature_id`, `text`, `normalized_text` | 商品描述/特征原始文本 |
| `Attribute` | `attribute_id`, `name`, `value`, `attribute_type` | LLM 提取的结构化属性 |

`Attribute` 的 `attribute_type` 取值范围：`benefit`、`skin_type`、`scent`、`texture`、`ingredient`、`material`、`color`、`size`、`target_area`、`usage`、`brand`、`product_type`、`other`

### 关系

| 关系类型 | 方向 | 属性 | 说明 |
|---|---|---|---|
| `WROTE` | User → Review | — | 用户写了这条评论 |
| `REVIEWS` | Review → Product | — | 评论指向的商品 |
| `RATED` | User → Product | `rating`, `timestamp`, `verified_purchase` | 推荐查询的快捷边 |
| `BELONGS_TO` | Product → Category | — | 商品所属类别 |
| `SOLD_BY` | Product → Store | — | 商品出品方 |
| `HAS_FEATURE` | Product → Feature | — | 商品原始特征文本 |
| `HAS_ATTRIBUTE` | Product → Attribute | `confidence`, `evidence`, `model` | LLM 提取的结构化属性 |

### 图结构概览

```
User -[WROTE]-> Review -[REVIEWS]-> Product
User -[RATED]-> Product
Product -[BELONGS_TO]-> Category
Product -[SOLD_BY]-> Store
Product -[HAS_FEATURE]-> Feature
Product -[HAS_ATTRIBUTE]-> Attribute
```

## 数据规模

| 实体 | 数量 |
|---|---|
| Product | 112,590 |
| User | 168,659 |
| Review | 200,000 |
| Store | 30,361 |
| Feature | 89,666 |
| Category | 2 |
| Attribute（已提取） | ~1,000 个商品 |

---

## 构建基础图谱

**默认构建**（全部商品元数据 + 前 200,000 条评论）：

```bash
python3 scripts/build_kg_csv.py
```

**小规模图谱**（适用于 Neo4j Aura 免费版 200k 节点限制）：

```bash
python3 scripts/build_kg_csv.py \
  --max-reviews 30000 \
  --max-meta 20000 \
  --max-features-per-product 10 \
  --output-dir kg_output/all_beauty_aura_small
```

输出目录：`kg_output/all_beauty/`

---

## 提取商品属性（LLM）

从商品元数据中用 LLM 提取结构化属性，写入 JSONL：

```bash
python3 scripts/extract_product_attributes_llm.py \
  --provider deepseek \
  --limit 1000 \
  --resume \
  --compact-input \
  --skip-sparse \
  --batch-size 5
```

建议先用 `--limit 20` 检查输出质量，再扩大规模。`--resume` 支持断点续跑。

转换成 Neo4j CSV：

```bash
python3 scripts/attributes_to_kg_csv.py \
  --input-path kg_output/attributes/product_attributes_llm.jsonl
```

输出：`kg_output/all_beauty/nodes_attributes.csv` 和 `rel_product_attribute.csv`

---

## 导入 Neo4j

### 本地 Neo4j（Docker）

```bash
docker run -d --name neo4j-am \
  -p 7474:7474 -p 7687:7687 \
  -e NEO4J_AUTH=neo4j/password123 \
  neo4j:latest
```

设置 `.env`：
```
NEO4J_URI=bolt://localhost:7687
NEO4J_USERNAME=neo4j
NEO4J_PASSWORD=password123
NEO4J_DATABASE=neo4j
```

### Bolt 批量导入（本地/Aura 通用）

先在 `.env` 配置好连接信息，然后：

```bash
# 导入基础图谱
python3 scripts/import_kg_to_neo4j.py

# 导入 LLM 属性（基础图谱导入完成后运行）
python3 scripts/import_attributes_to_neo4j.py
```

### 本地 Neo4j Cypher 文件导入

把 `kg_output/all_beauty/` 复制到 Neo4j 的 `import` 目录，然后在 Browser 或 cypher-shell 中运行：

```
neo4j/all_beauty_import.cypher
```

### 通过 GitHub Raw URL 导入 Aura

先生成小于 25MB 的分片文件：

```bash
python3 scripts/split_csv_for_aura_github.py \
  --base-url https://raw.githubusercontent.com/USER/REPO/main
```

上传 `kg_output/all_beauty_github/all_beauty/` 到公开 GitHub 仓库，然后运行 `neo4j/all_beauty_import_github_chunks.cypher`。

---

## 推荐 API

基于知识图谱的自然语言商品推荐，详见 [README.md](README.md)。

启动：
```bash
uvicorn api.main:app --reload
```

核心推荐流程：
1. LLM 将用户自然语言查询解析为结构化属性过滤条件
2. Cypher 查询匹配 `HAS_ATTRIBUTE` 边，按 `confidence × weight` 加权打分
3. 返回推荐列表，每条结果附带匹配到的属性作为推荐根据

---

## Cypher 推荐查询示例

基于共享特征的内容推荐（Feature 节点）：

```cypher
MATCH (u:User {user_id: $user_id})-[r:RATED]->(p:Product)-[:HAS_FEATURE]->(f:Feature)<-[:HAS_FEATURE]-(rec:Product)
WHERE r.rating >= 4 AND rec <> p
RETURN rec.product_id, rec.title,
       count(DISTINCT f) AS shared_features,
       collect(DISTINCT f.text)[0..5] AS evidence
ORDER BY shared_features DESC, rec.average_rating DESC
LIMIT 20
```

基于结构化属性的内容推荐（Attribute 节点）：

```cypher
MATCH (p:Product)-[r:HAS_ATTRIBUTE]->(a:Attribute)
WHERE a.attribute_type = 'skin_type' AND a.value CONTAINS 'dry'
RETURN p.product_id, p.title, p.average_rating,
       r.confidence, r.evidence
ORDER BY r.confidence DESC, p.average_rating DESC
LIMIT 20
```

协同过滤（相似用户推荐）：

```cypher
MATCH (u:User {user_id: $user_id})-[r1:RATED]->(p:Product)<-[r2:RATED]-(similar:User)
WHERE r1.rating >= 4 AND r2.rating >= 4 AND similar <> u
WITH similar, count(p) AS shared_products
ORDER BY shared_products DESC LIMIT 20
MATCH (similar)-[r3:RATED]->(rec:Product)
WHERE r3.rating >= 4
  AND NOT (u)-[:RATED]->(rec)
RETURN rec.product_id, rec.title,
       count(DISTINCT similar) AS recommended_by,
       avg(r3.rating) AS avg_similar_rating
ORDER BY recommended_by DESC, avg_similar_rating DESC
LIMIT 20
```

查询推荐根据的图路径：

```cypher
MATCH path = (u:User {user_id: $user_id})-[:RATED]->(:Product)
             -[:HAS_ATTRIBUTE]->(:Attribute)<-[:HAS_ATTRIBUTE]-
             (rec:Product {product_id: $product_id})
RETURN path
LIMIT 5
```
