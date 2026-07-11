# グラフスキーマ定義

バックエンド担当はこのルールに基づいてグラフデータベースを構築する。
ジャンル（Amazon カテゴリ）を変えても同じスキーマが使えるよう設計している。
ジャンル依存の設定は `config.yaml` で管理する。

---

## ノード（7種）

### 商品（Product）
| プロパティ | データ元 | 型 | 備考 |
|-----------|---------|-----|------|
| product_id | parent_asin | string | PK |
| title | title | string | 商品名 |
| price | price | float | 欠損あり |
| avg_rating | average_rating | float | |
| rating_count | rating_number | int | |
| description | features + description を連結 | string | 生テキスト。LLM属性抽出とFull-Text検索に使う |
| image_url | images | string | 欠損あり。`backfill_display_fields.py --images` で後付け |
| title_ja | title（LLM翻訳） | string | 欠損あり。`backfill_display_fields.py --titles-ja` で後付け。表示言語が日本語のときのみAPIが使う |

### ユーザ（User）
| プロパティ | データ元 | 型 | 備考 |
|-----------|---------|-----|------|
| user_id | user_id | string | PK |

### レビュー（Review）
| プロパティ | データ元 | 型 | 備考 |
|-----------|---------|-----|------|
| review_id | 生成 ID | string | SHA1(user_id\|product_id\|timestamp)，PK |
| rating | rating | float | 1–5 |
| timestamp | timestamp | int | Unix ms |
| helpful_vote | helpful_vote | int | |
| verified | verified_purchase | bool | |
| title | title | string | レビュータイトル |
| text | text | string | レビュー本文（生テキスト。LLM属性抽出に使う） |
| title_ja | title（LLM翻訳） | string | 欠損あり。`backfill_display_fields.py --reviews-ja` で後付け |
| text_ja | text（LLM翻訳） | string | 欠損あり。`backfill_display_fields.py --reviews-ja` で後付け。表示言語が日本語のときのみ`get_reviews()`が使う |

### カテゴリ（Category）
| プロパティ | データ元 | 型 | 備考 |
|-----------|---------|-----|------|
| category_id | 生成 ID | string | SHA1(name.lower())，PK |
| name | main_category / categories | string | カテゴリ名 |
| level | 階層深さ | int | 0=メイン，1=サブ，… |

### ブランド（Brand）
| プロパティ | データ元 | 型 | 備考 |
|-----------|---------|-----|------|
| brand_id | 生成 ID | string | SHA1(name.lower())，PK |
| name | store | string | Amazon の store フィールドをブランドとして扱う |

> **注**: Amazon Reviews'23 の生データに独立した brand フィールドは存在しない。`store` フィールドはブランドの Amazon ストアフロント名に相当するため Brand ノードとして扱う。

### 商品属性（Attribute）
| プロパティ | 型 | 備考 |
|-----------|-----|------|
| attribute_id | string | SHA1(attr_type\|value)，PK |
| attr_type | string | ジャンル非依存。LLM抽出または `details` のルールベース抽出で自動生成 |
| value | string | 属性値（小文字・正規化済み） |
| value_ja | string | 欠損あり。`backfill_display_fields.py --values-ja` で後付け。表示言語が日本語のとき、Text2Cypherが生成するCypherがmatched_attrsに含めていれば`value`の代わりに使われる |

> **ジャンル非依存の仕組み**: `attr_type` は LLM が自由に命名するか、`extract_product_attributes.py` が metadata の `details` キーを自動で snake_case 化して生成する（手動の対応表は持たない）。config.yaml にジャンル別の属性リストは持たない。プロンプトで「snake_case・既存の型を再利用」と指示することに加え、抽出完了後に `canonicalize_attributes.py` が全体の attr_type／value を見て同義語の統合マップを LLM に作らせ、`build_attribute_graph.py` が適用することで表記ゆれを解消する（例: `item_form` と `texture` の統合）。スキーマ・エッジ・Cypher クエリはジャンルによらず共通。

### 検索ログ（SearchLog）
| プロパティ | 型 | 備考 |
|-----------|-----|------|
| log_id | string | UUID，PK |
| query | string | 検索クエリ文字列。ホーム推薦時は `"[home]"` |
| cypher | string | LLM が生成した Cypher |
| explanation | string | Cypher の一文説明 |
| result_product_ids | string[] | 検索結果の product_id 一覧 |
| result_count | int | 検索結果件数 |
| timestamp | int | Unix ms |

> パーソナライゼーション（ユーザ文脈・過去の成功クエリの few-shot）のために `app/api/recommender.py` の `log_search()` が書き込む。ユーザごとに直近 30 件のみ保持し、古いものは削除する。

---

## エッジ（11種）

### RATED：User → Product
協調フィルタリングの中核エッジ。User→Product 間のショートカット。

| エッジ属性 | 型 | 備考 |
|-----------|-----|------|
| rating | float | 評価点（1–5） |
| timestamp | int | Unix ms |

### WROTE：User → Review
ユーザがレビューを書いた関係。属性なし。

### ABOUT：Review → Product
レビューが対象商品についての関係。属性なし。

> Am/ の `REVIEWS` エッジを改名（語義を明確化）。

### BELONGS_TO：Product → Category
商品がカテゴリに属する関係。属性なし。

### SUBCATEGORY_OF：Category → Category
カテゴリの親子階層関係。可変長パスクエリ（`*1..N`）を使った階層横断を可能にする。属性なし。

### MADE_BY：Product → Brand
商品がブランドに紐づく関係。属性なし。

### HAS_ATTRIBUTE：Product → Attribute
商品説明テキストから LLM が抽出した属性。属性なし。

> confidence は抽出時（`build_attribute_graph.py`）のフィルタ（`min_confidence`未満は
> エッジを作らない）にのみ使い、エッジのプロパティとしては保持しない。閾値を超えた
> エッジ同士での確信度の差は商品の関連性を意味しないため、検索スコアには使わない
> （代わりに一致した属性の件数を使う）。evidence/source/modelも同様の理由で保持しない
> （抽出結果のデバッグは`product_attributes.jsonl`を直接参照する）。

### MENTIONS：Review → Attribute
レビューテキストから LLM が抽出した属性言及。商品説明にない「ユーザ検証済み」属性を表現する。

| エッジ属性 | 型 | 備考 |
|-----------|-----|------|
| sentiment | string | "positive" / "negative" / "neutral" |

> confidence は HAS_ATTRIBUTE と同じ理由でエッジのプロパティとしては保持しない
> （`build_attribute_graph.py`の抽出時フィルタ（`min_confidence`未満は作らない）にのみ使う）。

### VIEWED：User → Product
ユーザが商品を閲覧した関係。パーソナライゼーションの行動ログ。

| エッジ属性 | 型 | 備考 |
|-----------|-----|------|
| timestamp | int | Unix ms |
| search_id | string\|null | 閲覧元の SearchLog.log_id（あれば） |

> ユーザごとに直近 20 件のみ保持し、古いものは削除する。

### SEARCHED：User → SearchLog
ユーザが検索を実行した関係。属性なし。

---

## グラフ構造の概略

```
User  -[WROTE]->        Review -[ABOUT]->        Product
User  -[RATED]->                                 Product
User  -[VIEWED]->                                Product
User  -[SEARCHED]->     SearchLog
                        Review -[MENTIONS]->      Attribute
                                                 Product -[HAS_ATTRIBUTE]-> Attribute
                                                 Product -[BELONGS_TO]->   Category
                                                 Product -[MADE_BY]->      Brand
Category -[SUBCATEGORY_OF]-> Category
```

---

## 代表的な多段推論クエリ

### 推薦器の最小元パス（実装済み）
現行APIの主経路は、LLMにCypher全文を書かせるのではなく、LLMが会話を
`product_keywords` / `category_keywords` / `attribute_keywords` へ構造化し、
Neo4j側では固定の元パスを実行する。

```cypher
MATCH (u:User {user_id: $uid})-[r:RATED|VIEWED]->(seed:Product)
      -[:HAS_ATTRIBUTE]->(a:Attribute)
      <-[:HAS_ATTRIBUTE]-(rec:Product)
WHERE NOT (u)-[:RATED|VIEWED]->(rec)
RETURN rec, collect(DISTINCT a.value) AS matched_attrs
ORDER BY size(matched_attrs) DESC LIMIT $limit
```

実際のAPIでは、この元パスに加えて `Product.title/title_ja`、`BELONGS_TO`、
`HAS_ATTRIBUTE` を会話条件でフィルタし、`avg_rating` と `rating_count` も
ランキングに加える。さらに、オフライン比較実験で協調フィルタリングが exact-ASIN
予測に強いことが確認されたため、現行APIでは peer collaborative meta-path に加えて、
直近の高評価商品を起点にした transition-first meta-path を主なランキング信号として
加えている。

```cypher
MATCH (u:User {user_id: $uid})-[sr:RATED]->(seed:Product)
      <-[pr:RATED]-(peer:User)-[cr:RATED]->(rec:Product)
WHERE sr.rating >= 4 AND pr.rating >= 4 AND cr.rating >= 4
  AND peer <> u
RETURN rec, count(DISTINCT peer) AS peer_support
ORDER BY peer_support DESC LIMIT $limit
```

現在の上位ランキングでは、特に以下の「似たユーザがその後に高評価した商品」を優先する。

```cypher
MATCH (u:User {user_id: $uid})-[sr:RATED]->(seed:Product)
WHERE sr.rating >= 4
WITH u, seed, sr
ORDER BY sr.timestamp DESC
LIMIT 15
MATCH (seed)<-[pr:RATED]-(peer:User)-[tr:RATED]->(rec:Product)
WHERE peer <> u
  AND pr.rating >= 4
  AND tr.rating >= 4
  AND tr.timestamp > pr.timestamp
  AND NOT (u)-[:RATED|VIEWED]->(rec)
RETURN rec, count(DISTINCT peer) AS transition_support
ORDER BY transition_support DESC LIMIT $limit
```

この設計は、単なる属性一致よりも「次に選ばれやすい商品」を前方に出すためのもので、
属性元パスは会話条件のフィルタと推薦理由に使う。

`MENTIONS` はレビューで確認された属性を使う次段階の拡張候補。

### ① 4ホップ協調フィルタリング
```cypher
MATCH (u:User {user_id: $uid})-[:RATED]->(seen:Product)
      <-[:RATED]-(peer:User)-[:RATED]->(rec:Product)
WHERE peer <> u AND NOT (u)-[:RATED]->(rec)
RETURN rec, count(peer) AS support
ORDER BY support DESC LIMIT $limit
```

### ② 属性ベース類似推薦（3ホップ）
```cypher
MATCH (u:User {user_id: $uid})-[:RATED {rating: 4}]->(p:Product)
      -[:HAS_ATTRIBUTE]->(a:Attribute)
      <-[:HAS_ATTRIBUTE]-(rec:Product)
WHERE rec <> p AND NOT (u)-[:RATED]->(rec)
RETURN rec, collect(DISTINCT a.value) AS matched_attrs
ORDER BY size(matched_attrs) DESC LIMIT $limit
```

### ③ カテゴリ階層横断（可変長パス）
```cypher
MATCH (p:Product)-[:BELONGS_TO]->(:Category)
      -[:SUBCATEGORY_OF*1..3]->(main:Category {name: $category})
RETURN p LIMIT $limit
```

### ④ ユーザ検証済み属性推薦（MENTIONS 活用）
```cypher
MATCH (u:User)-[:WROTE]->(r:Review)-[:ABOUT]->(p:Product),
      (r)-[:MENTIONS {sentiment: "positive"}]->(a:Attribute {attr_type: $attr_type, value: $value})
WHERE r.rating >= 4
RETURN p, count(r) AS evidence_count
ORDER BY evidence_count DESC LIMIT $limit
```

### ⑤ 商品説明 × レビュー両方で確認された属性を持つ商品
```cypher
MATCH (p:Product)-[:HAS_ATTRIBUTE]->(a:Attribute)
      <-[:MENTIONS {sentiment: "positive"}]-(r:Review)-[:ABOUT]->(p)
WHERE a.attr_type = $attr_type
RETURN p, a.value, count(r) AS user_confirmations
ORDER BY user_confirmations DESC LIMIT $limit
```

---

## Neo4j インデックス

```cypher
CREATE CONSTRAINT product_id_unique IF NOT EXISTS FOR (n:Product) REQUIRE n.product_id IS UNIQUE;
CREATE CONSTRAINT user_id_unique IF NOT EXISTS FOR (n:User) REQUIRE n.user_id IS UNIQUE;
CREATE CONSTRAINT review_id_unique IF NOT EXISTS FOR (n:Review) REQUIRE n.review_id IS UNIQUE;
CREATE CONSTRAINT category_id_unique IF NOT EXISTS FOR (n:Category) REQUIRE n.category_id IS UNIQUE;
CREATE CONSTRAINT brand_id_unique IF NOT EXISTS FOR (n:Brand) REQUIRE n.brand_id IS UNIQUE;
CREATE CONSTRAINT attribute_id_unique IF NOT EXISTS FOR (n:Attribute) REQUIRE n.attribute_id IS UNIQUE;
CREATE CONSTRAINT log_id_unique IF NOT EXISTS FOR (n:SearchLog) REQUIRE n.log_id IS UNIQUE;

-- Attribute 検索用
CREATE INDEX attr_type  IF NOT EXISTS FOR (a:Attribute) ON (a.attr_type);
CREATE INDEX attr_value IF NOT EXISTS FOR (a:Attribute) ON (a.value);

-- Full-Text 検索用
CREATE FULLTEXT INDEX product_description_ft IF NOT EXISTS FOR (n:Product) ON EACH [n.title, n.description];
CREATE FULLTEXT INDEX review_text_ft IF NOT EXISTS FOR (n:Review) ON EACH [n.title, n.text];
```

---

## 実装上の注意

- `review_id` は `SHA1(user_id|product_id|timestamp)` で生成する（リビルド時の ID 安定性のため行インデックスは使わない）。
- `attribute_id` は `SHA1(attr_type|value)` で生成する（同じ属性は同一ノードとして共有される）。
- `Product.description` は `features` と `description` フィールドを空白で連結した生テキスト。
- `HAS_ATTRIBUTE` は `extract_product_attributes.py` + `build_attribute_graph.py` で、`MENTIONS` は `extract_review_mentions.py` + `build_attribute_graph.py` で生成する。
- `SearchLog` ノードと `SEARCHED`／`VIEWED` エッジは、KG構築パイプラインではなく `app/api/recommender.py`（`log_search()`／`log_view()`）が API 呼び出し時に書き込む。パーソナライゼーション（ユーザ文脈・過去の成功クエリの few-shot）専用のデータで、Text2Cypher の商品検索クエリ自体は対象としない。
- データ規模は `config.yaml` の `scale` セクションで制御する。
- Am/ の `Feature` ノード・`HAS_FEATURE` エッジは廃止。テキストは `Product.description` に統合。
