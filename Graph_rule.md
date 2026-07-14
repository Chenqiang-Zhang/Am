# グラフスキーマ定義

バックエンド担当はこのルールに基づいてグラフデータベースを構築する。
ジャンル（Amazon カテゴリ）を変えても同じスキーマが使えるよう設計している。
ジャンル依存の設定は `config.yaml` で管理する。

---

## ノード（6種）

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
| attr_type | string | ジャンル非依存。`attr_vocab.yaml`（成長する語彙データベース）の(attr_type, value)組み合わせを再利用優先で選ぶ |
| value | string | 属性値（小文字・正規化済み）。attr_typeと組み合わせ単位で同じデータベースを参照 |
| value_ja | string | 欠損あり。`backfill_display_fields.py --values-ja` で後付け。表示言語が日本語のとき、Text2Cypherが生成するCypherがmatched_attrsに含めていれば`value`の代わりに使われる |

> **ジャンル非依存の仕組み**: `propose_attr_vocab.py`は独立した2つの経路でシードを提案し、2段階でマージする。①メタデータ経路：商品メタデータの`details`辞書のキー・実際の値をLLM抜きで決定的に走査し、その結果だけを使ってLLMに1回、attr_type/valuesと`raw_key→attr_type`の対応（`detail_key_map.yaml`）を同時に提案させる。②サンプル経路：商品・レビューの自由文サンプルだけを使ってLLMに1回、自由文にしか出てこない属性を提案させる（メタデータ経路の結果には依存しない）。両者は完全に独立に実行できる。マージは2段階：まず同名attr_typeのvaluesを機械的に合併（LLM不使用）、その後さらにLLMに1回、似た概念だが名前が違うattr_typeの統合、使い物にならないattr_typeの削除、名前・説明・valueの表記整理をさせる（新しい値を捏造することは禁止、既存の値の整理・統合のみ）。`detail_key_map.yaml`もこの統合後の最終名に付け替えられる。人手でのレビューはこの統合後に行う。**すべてのattr_typeに閉じたvaluesリストを持たせる**（自由記述として`values`を空にするattr_typeは提案しない — 1attr_typeあたりのvalue数は`--min-values-per-type`/`--max-values-per-type`で制御し、規定数に満たない候補はシードから除外され実行時に警告される）。本番の抽出（`extract_product_attributes.py`・`extract_review_mentions.py`）では、LLMは既存の(attr_type, value)組み合わせの再利用を強く指示され、本当に何も当てはまらない場合のみ新規の組み合わせを提案できる（type・valueどちらの新規追加にも対応）。新規に受理された組み合わせはその場で同じ`attr_vocab.yaml`に書き戻され、以降のバッチ・実行でも「既知」として再利用される（`utils/attr_vocab.py`の`GrowableVocab`、スレッドセーフ）。抽出の実行末尾に成長件数のサマリが出るので、増えすぎていないか人手で確認する。`details`キーは`detail_key_map.yaml`（同じくpropose_attr_vocab.pyが提案、こちらは実行時に成長しない静的なマップ）を通してattr_vocab.yamlの語彙にマッピングされ、対応の無いキーは無視される。config.yamlにジャンル別の属性リストは持たない — ジャンルを変えたら`propose_attr_vocab.py`をそのジャンルのデータに対して再実行し、新しい`attr_vocab.yaml`/`detail_key_map.yaml`を作る。スキーマ・エッジ・Cypherクエリ自体はジャンルによらず共通。

---

## エッジ（9種）

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

> **廃止**: `VIEWED`（User→Product、閲覧履歴）・`SEARCHED`（User→SearchLog）・`SearchLog`ノードはすべて廃止した。
> 個人化の根拠は`RATED`のみとし、閲覧履歴・検索履歴などRATED以外のユーザー行動は一切保持しない方針のため。

---

## グラフ構造の概略

```
User  -[WROTE]->        Review -[ABOUT]->        Product
User  -[RATED]->                                 Product
                        Review -[MENTIONS]->      Attribute
                                                 Product -[HAS_ATTRIBUTE]-> Attribute
                                                 Product -[BELONGS_TO]->   Category
                                                 Product -[MADE_BY]->      Brand
Category -[SUBCATEGORY_OF]-> Category
```

---

## 代表的な多段推論クエリ

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
- データ規模は `config.yaml` の `scale` セクションで制御する。
- Am/ の `Feature` ノード・`HAS_FEATURE` エッジは廃止。テキストは `Product.description` に統合。
