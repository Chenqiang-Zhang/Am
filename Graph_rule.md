# グラフの構築ルール
バックエンド担当はこのルールに基づいてグラフデータベースを構築．

---

## ノード

### ユーザ（User）
| プロパティ | フィールド | 備考 |
|-----------|-----------|------|
| user_id | user_id | PK |

### 商品（Product）
| プロパティ | フィールド | 備考 |
|-----------|-----------|------|
| product_id | parent_asin | PK |
| title | title | 商品名 |
| main_category | main_category | メインカテゴリ名（非正規化済み） |
| price | price | 数値，欠損あり |
| average_rating | average_rating | 数値 |
| rating_number | rating_number | 整数 |

> **brand について**：Amazon Reviews'23 の生データには独立した brand フィールドが存在しない．`store` フィールドをブランド／出品者の代替として Store ノードで管理する．LLM 属性抽出で `brand` を Attribute ノードとして補完可能．

### レビュー（Review）
| プロパティ | フィールド | 備考 |
|-----------|-----------|------|
| review_id | 生成 ID | SHA1(user_id\|product_id\|timestamp)，PK |
| title | title | レビュータイトル |
| text | text | レビュー本文 |
| rating | rating | 評価点（1–5） |
| timestamp | timestamp | Unix ms |
| helpful_vote | helpful_vote | 「参考になった」票数 |
| verified_purchase | verified_purchase | 購入確認済みフラグ（bool） |

> `asin`（商品 ID）はレビューノードのプロパティではなく REVIEWS エッジで表現する．

### カテゴリ（Category）
| プロパティ | フィールド | 備考 |
|-----------|-----------|------|
| category_id | 生成 ID | SHA1(name)，PK |
| name | main_category / categories | カテゴリ名 |

### ストア（Store）
| プロパティ | フィールド | 備考 |
|-----------|-----------|------|
| store_id | 生成 ID | SHA1(name)，PK |
| name | store | ブランド／出品者名として扱う |

### 特徴テキスト（Feature）
商品メタデータの `features` / `description` フィールドから抽出した生テキスト．

| プロパティ | 備考 |
|-----------|------|
| feature_id | SHA1(normalized_text)，PK |
| text | 元テキスト |
| normalized_text | 小文字・記号除去済みテキスト |

### 商品属性（Attribute）※ LLM 抽出
LLM（OpenAI / DeepSeek）が商品メタデータから構造化して抽出した属性．Feature とは別ノード．

| プロパティ | 備考 |
|-----------|------|
| attribute_id | SHA1(attribute_type\|name\|value)，PK |
| name | 属性名 |
| value | 属性値 |
| attribute_type | benefit / skin_type / scent / texture / ingredient / material / color / size / target_area / usage / brand / product_type / other |

---

## エッジ

### WROTE：User → Review
ユーザがレビューを書いた関係．エッジ属性なし．

### REVIEWS：Review → Product
レビューが対象商品を評価している関係．エッジ属性なし．

### RATED：User → Product
推薦クエリの高速化のため，User→Product 間に直接張るショートカットエッジ．

| エッジ属性 | フィールド | 備考 |
|-----------|-----------|------|
| rating | rating | 評価点 |
| timestamp | timestamp | Unix ms |
| verified_purchase | verified_purchase | 購入確認済みフラグ |

### BELONGS_TO：Product → Category
商品がカテゴリに属する関係．エッジ属性なし．

### SOLD_BY：Product → Store
商品がストア（ブランド）から販売されている関係．エッジ属性なし．

### HAS_FEATURE：Product → Feature
商品が特徴テキストを持つ関係．エッジ属性なし．

### HAS_ATTRIBUTE：Product → Attribute
LLM 抽出属性との関係．

| エッジ属性 | 備考 |
|-----------|------|
| confidence | LLM の確信度（0–1） |
| evidence | 根拠テキスト |
| model | 使用モデル名（例：deepseek-chat） |

---

## グラフ構造の概略

```
User -[WROTE]-> Review -[REVIEWS]-> Product
User -[RATED]-> Product
Product -[BELONGS_TO]-> Category
Product -[SOLD_BY]-> Store
Product -[HAS_FEATURE]-> Feature
Product -[HAS_ATTRIBUTE]-> Attribute
```

---

## 実装上の注意

- `review_id` は `SHA1(user_id|product_id|timestamp)` で生成する．行インデックスは含めない（リビルド時の ID 安定性のため）．
- Feature ノードは `--max-features-per-product`（デフォルト 20）で商品ごとの上限を設ける．
- Attribute ノードは LLM 抽出後に別スクリプト（`attributes_to_kg_csv.py`）で CSV 化し，`import_attributes_to_neo4j.py` でインポートする．ベースグラフとは独立して追加可能．
- Aura 無料枠（ノード上限 200k）向けには `--max-reviews 30000 --max-meta 20000` で小規模グラフを構築する．
