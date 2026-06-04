# Amazon Reviews'23 Knowledge Graph Recommender

这是一个基于 Amazon Reviews'23 `All_Beauty` 类目的商品推荐系统实验项目。当前目标是先把评论、商品元数据转换成 Neo4j 可导入的知识图谱，后续再接入 LLM 做属性抽取、需求解析和推荐解释生成。

## 当前图谱

第一版图谱包含：

- `User`
- `Product`
- `Review`
- `Category`
- `Store`
- `Feature`

主要关系：

- `(User)-[:WROTE]->(Review)`
- `(Review)-[:REVIEWS]->(Product)`
- `(User)-[:RATED]->(Product)`
- `(Product)-[:BELONGS_TO]->(Category)`
- `(Product)-[:SOLD_BY]->(Store)`
- `(Product)-[:HAS_FEATURE]->(Feature)`

更详细的建图说明、Neo4j 导入方式和推荐查询见 [KG_README.md](KG_README.md)。

## 推荐仓库结构

```text
.
├── README.md
├── KG_README.md
├── requirements.txt
├── data/                  # 本地原始数据，不提交 Git
├── kg_output/             # 本地生成 CSV，不提交 Git
├── neo4j/                 # Cypher 导入脚本
├── scripts/               # 数据处理和 CSV 构建脚本
└── data.ipynb             # 数据分析 notebook
```

建议这个仓库只做代码、文档、图谱 schema、Cypher 和实验记录的版本控制。原始数据和生成后的 CSV 比较大，建议放在本地、Git LFS、对象存储，或单独的数据发布仓库中。

## 快速开始

安装依赖：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

把 Amazon Reviews'23 文件放到本地：

```text
data/All_Beauty.jsonl.gz
data/meta_All_Beauty.jsonl.gz
```

生成 Neo4j CSV：

```bash
python3 scripts/build_kg_csv.py
```

如果使用 Neo4j Aura，并计划通过 GitHub raw URL 让 Aura 读取 CSV，可以生成小分片：

```bash
python3 scripts/split_csv_for_aura_github.py \
  --base-url https://raw.githubusercontent.com/USER/REPO/main
```

然后把 `kg_output/all_beauty_github/all_beauty/` 中的 CSV 上传到公开可访问的位置，并运行 `neo4j/all_beauty_import_github_chunks.cypher`。

## 后续方向

- 用 LLM 从商品标题、描述、评论中抽取更规范的属性节点，例如功效、肤质、气味、质地、适用场景。
- 用 LLM 把用户自然语言需求解析成图查询条件。
- 用 Neo4j 检索证据路径，再让 LLM 基于路径生成推荐理由。
