# 個人化ホーム推薦：制約付き Text2Cypher 設計

## 1. このブランチの目的

`daito/text2cypher-home-explanations` は、個人化ホーム推薦の候補取得を固定メタパスから制約付き Text2Cypher に変更した共有用ブランチである。

目的は次の2点。

- ユーザの評価・閲覧履歴に応じて、LLMが適切なグラフ経路を選べるようにする。
- 「どの商品を起点に、どの属性を共有しているか」を使って、商品ごとの自然な推薦理由を表示する。

変更対象は主に `POST /recommend/home` であり、対話型推薦の ASK / SEARCH フローはこの変更の対象外。

## 2. 全体フロー

```text
ユーザが個人化推薦を開く
  ↓
Neo4jからユーザ文脈を取得
  - 4以上の評価商品
  - 評価商品の属性
  - 最近の閲覧商品
  - 嗜好属性と最近の検索文
  ↓
LLMが P1〜P5 からグラフ経路を選び Cypher を生成
  ↓
サーバが Cypher を無害化・検証
  - 書き込み句を禁止
  - 使用可能なパラメータを限定
  - 必須の返却フィールドを確認
  - Neo4j EXPLAIN で実行可能性を確認
  ↓
Neo4jで候補商品と商品ごとの根拠を取得・順位付け
  ↓
LLMが最終的に実行した Cypher 全体を説明
  ↓
LLMが商品ごとの推薦理由を生成
  ↓
UIに候補・推薦理由・開発者向け根拠を表示
```

履歴がないユーザには LLM を呼ばず、人気・評価ベースのフォールバックを返す。

## 3. LLMが選べるグラフ経路

LLMに自由な Cypher を書かせるのではなく、次の P1〜P5 の中から1つの主経路と、必要な場合のみ1つの補助経路を選ばせる。

### P1：高評価商品との属性類似

```text
User -> RATED -> Seed Product -> HAS_ATTRIBUTE -> Attribute
                                              <- HAS_ATTRIBUTE <- Candidate Product
```

例：高く評価したマリオ商品と、フランチャイズやゲームモードが共通する商品を探す。

### P2：類似ユーザの高評価商品

```text
User -> RATED -> Shared Product <- RATED <- Peer User -> RATED -> Candidate Product
```

同じ商品を高く評価した他ユーザが、別に高く評価した商品を探す。

### P3：類似ユーザの時系列遷移

P2と同じ構造を使うが、候補商品の評価時刻が共通商品の評価時刻より後であることを条件にする。

### P4：肯定的レビューで裏付けられた共通属性

```text
User -> RATED -> Seed Product -> HAS_ATTRIBUTE -> Attribute
                                              <- MENTIONS <- Review -> ABOUT -> Candidate Product
```

高く評価した商品と共通する属性が、候補商品の肯定的レビューでも言及されているかを使う。

### P5：カテゴリ・ブランドの親和性

```text
User -> RATED -> Seed Product -> BELONGS_TO -> Category <- BELONGS_TO <- Candidate Product
User -> RATED -> Seed Product -> MADE_BY    -> Brand    <- MADE_BY    <- Candidate Product
```

属性や類似ユーザの根拠が少ない場合の補完経路として使う。

## 4. LLMに渡すデータ

個人化ホーム推薦では、用途の異なる3回の LLM 処理がある。

| 処理 | LLMに渡す主なデータ | 出力 |
|---|---|---|
| Text2Cypher | グラフスキーマ、属性語彙、P1〜P5、few-shot、高評価商品と属性、閲覧履歴、嗜好属性、最近の検索文 | 読み取り専用 Cypher |
| 実行 Cypher の説明 | 検証・実行された最終 Cypher | 利用履歴、グラフ経路、除外条件、順位付けの説明 |
| 商品ごとの理由 | 候補商品名、推薦経路、起点商品、一致属性、根拠数 | 商品カード用の自然な1文 |

Text2Cypher 用のユーザ文脈は次の上限で絞っている。

- 4以上の評価商品：最大6件、各1商品に属性最大8件
- 最近の閲覧商品：最大4件
- 推定嗜好属性：最大8件
- 最近の検索文：最大5件
- グラフ内の属性種類：頻度上位20種類、各例最大6件

LLM には実際の `user_id` 値を渡さない。Cypher 内には `$uid` のみを書かせ、実値は Neo4j 実行時にサーバがバインドする。レビュー本文、商品説明全文、他ユーザの履歴一式、DB接続情報もプロンプトには渡さない。

## 5. 同じ Cypher で商品ごとに理由が異なる理由

Cypher は同じでも、各結果行に含まれる根拠は異なる。

```text
商品A
  seed_titles: [マリオ＆ルイージRPG]
  matched_attrs: [domain_franchise: Mario]

商品B
  seed_titles: [CRISIS CORE -FINAL FANTASY VII-]
  matched_attrs: [domain_franchise: Final Fantasy]
```

商品ごとの理由生成では、その行の `seed_titles`、`matched_attrs`、`reason_metrics` だけを根拠として LLM に渡す。そのため、検索方法は同じでも、マリオ商品と FF 商品で異なる推薦理由になる。

説明生成が失敗した場合は、同じ根拠データを使ったテンプレート文にフォールバックする。

## 6. Cypher の安全制約

生成された Cypher は、Neo4j での実行前に次を検証する。

- `CREATE` / `MERGE` / `DELETE` / `SET` などの書き込み操作がない。
- `db.*` / `dbms.*` / `apoc.*` プロシージャを呼ばない。
- パラメータは `$uid`, `$limit`, `$ignored_attr_types` だけを使う。
- 候補商品、スコア、根拠に必要な RETURN 別名をすべて含む。
- `ORDER BY score DESC LIMIT $limit` で終わる。
- `EXPLAIN` が成功した Cypher だけを実行する。

ローカル LLM が属性語彙を過剰な必須条件に変換する場合に備え、許可経路を変えない範囲で冗長な正の `attr_type` 制限を除去する。

## 7. 順位付けと属性の扱い

P1 の few-shot では、フランチャイズ、機種、遊び方・ジャンルなどを主な根拠とし、商品種別を候補の型保持に使う。価格施策、返品ポリシー、規約など推薦理由として弱い属性種類は `$ignored_attr_types` で除外する。

`domain_product_type` を共有する条件により、ゲーム本体の履歴からヘッドセットなどの周辺機器が、機種属性だけで上位に来ることを抑える。

## 8. UIで確認できる内容

### 通常ユーザ表示

- 推薦元（履歴ベース、対話条件など）
- 商品ごとの推薦理由
- 読みやすい属性タグ

### 開発者ビュー

- 最終的に実行した Cypher
- Cypher 全体の説明
- 利用した履歴
- グラフ経路
- 除外条件と順位付け根拠
- 商品ごとの起点商品、一致属性、根拠値

## 9. キャッシュとフォールバック

- 成功した個人化ホーム推薦は `user_id + lang + limit` 単位で1時間キャッシュする。
- キャッシュヒット時は LLM を呼ばない。
- Text2Cypher の生成、検証、実行、または0件の回避に失敗した場合は、人気・評価ベースの候補を返す。
- 商品理由の LLM 生成だけが失敗した場合は、候補取得結果を維持してテンプレート理由を表示する。

## 10. 実装の読みどころ

| 関心事 | ファイル・関数 |
|---|---|
| P1〜P5、few-shot、生成ルール | `app/api/recommender.py`: `_HOME_PATH_CATALOG`, `_HOME_TEXT2CYPHER_FEW_SHOTS`, `_HOME_TEXT2CYPHER_RULES` |
| ユーザ文脈の取得 | `app/api/recommender.py`: `_get_user_context()` |
| Cypher の生成・修正・実行 | `app/api/recommender.py`: `_get_or_generate_home()`, `_generate_cypher_and_execute()` |
| Cypher の安全検証 | `app/api/recommender.py`: `_sanitize_home_cypher()`, `_validate_cypher()` |
| 実行 Cypher の説明 | `app/api/recommender.py`: `_build_cypher_explanation_prompt()`, `_explain_executed_cypher()` |
| 商品ごとの推薦理由 | `app/api/recommender.py`: `_build_home_reason_prompt()`, `_generate_home_product_reasons()` |
| API レスポンス型 | `app/api/models.py` |
| クエリ説明表示 | `app/web/src/components/IntentPanel.tsx` |
| 推薦理由・根拠表示 | `app/web/src/components/RecommendationCard.tsx` |
| ホーム Text2Cypher の単体テスト | `tests/test_home_text2cypher.py` |

## 11. 発表用の短い説明

> ユーザの評価・閲覧履歴を基に、LLMが予め定義したグラフ経路から適切な経路を選び、読み取り専用の Cypher を生成します。実行前に安全性と出力形式を検証し、Neo4j が候補とグラフ上の根拠を返します。最後に、起点商品や共通属性から、商品ごとの推薦理由を生成します。

## 12. 注意点

- このブランチの Text2Cypher 変更は個人化ホーム推薦が主対象。対話型推薦は従来の ASK / SEARCH と構造化条件検索を保つ。
- 新しいグラフで `domain_franchise` などが欠ける場合でも一般属性を使えるが、シリーズ理由の質はグラフ内の属性品質に依存する。
- LLM が生成する候補取得ロジックの評価は、固定メタパスより再現性が低くなる可能性がある。そのため、使用経路の制約、`temperature: 0`、キャッシュ、開発者ビューを用いて確認可能性を高めている。
