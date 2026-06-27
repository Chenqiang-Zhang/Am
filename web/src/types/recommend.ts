// ============================================================================
// バックエンドとの契約（唯一の置き場）
//   api/models.py の Pydantic モデルと 1 対 1 で対応させる。
//   バックエンドのレスポンス形を変えたら "このファイルだけ" を直す。
//   （3 人開発での型の齟齬を防ぐための最重要ファイル）
// ============================================================================

/** POST /recommend のリクエスト body */
export interface RecommendRequest {
  query: string;
  limit: number;
}

/** LLM が抽出した属性フィルタ 1 件 */
export interface AttributeFilter {
  attribute_type: string;
  value: string;
  weight: number;
}

/** クエリの「システムによる解釈」 */
export interface SearchIntent {
  attribute_filters: AttributeFilter[];
  keywords: string[];
  price_max: number | null;
  min_rating: number | null;
}

/** 推薦根拠となった、商品に一致した構造化属性 1 件 */
export interface MatchedAttribute {
  attribute_type: string;
  name: string;
  value: string;
  confidence: number;
  evidence: string | null;
}

/** 推薦商品 1 件（説明付き） */
export interface Recommendation {
  product_id: string;
  title: string;
  image_url: string | null;
  price: number | null;
  average_rating: number | null;
  rating_number: number | null;
  score: number;
  matched_attributes: MatchedAttribute[];
  matched_terms: string[];
  matched_feature_evidence: string[];
  /** スコア内訳。キー例: attribute_match / feature_text_match / field_match /
   *  rating_quality / popularity / query_coverage（0〜1） */
  score_breakdown: Record<string, number>;
  explanation: string;
}

/** POST /recommend のレスポンス */
export interface RecommendResponse {
  query: string;
  intent: SearchIntent;
  recommendations: Recommendation[];
}

// ===== 対話型推薦（CRS）=====
export interface ChatMessage {
  role: "user" | "assistant";
  content: string;
}

// ===== レビュー =====
export interface ReviewItem {
  title: string | null;
  text: string;
  rating: number | null;
  helpful_vote: number | null;
  verified_purchase: boolean | null;
}

export interface ReviewsResponse {
  product_id: string;
  reviews: ReviewItem[];
}

/** POST /chat のレスポンス */
export interface ChatResponse {
  action: "ask" | "search";
  question: string | null;
  options: string[];
  preference_summary: string[];
  intent: SearchIntent | null;
  recommendations: Recommendation[];
}
