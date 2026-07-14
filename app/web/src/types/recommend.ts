// ============================================================================
// バックエンドとの契約（唯一の置き場）
//   api/models.py の Pydantic モデルと 1 対 1 で対応させる。
//   バックエンドのレスポンス形を変えたら "このファイルだけ" を直す。
//   （3 人開発での型の齟齬を防ぐための最重要ファイル）
// ============================================================================

/** POST /recommend のリクエスト body */
export interface RecommendRequest {
  query: string;
  user_id?: string | null;
  limit: number;
  lang?: "ja" | "en";
}

/** POST /recommend/home のリクエスト body */
export interface HomeRecommendRequest {
  user_id: string | null; // null = 非個人化モード（人気商品フォールバックのみ）
  limit: number;
  lang?: "ja" | "en";
}

/** クエリの「システムによる解釈」（構造化条件/元パス検索、またはfallback時のCypherと説明） */
export interface SearchIntent {
  cypher: string;
  cypher_explanation: string;
  applied_conditions: string[];
  condition_source: "llm" | "heuristic_fallback" | "none";
  retrieval_status: "matched" | "matched_after_relaxation" | "no_match" | "fallback_popular";
  no_result_reason: string | null;
  candidate_count: number;
  hard_conditions: string[];
  soft_conditions: string[];
}

/** 推薦根拠となった、商品に一致した構造化属性 1 件 */
export interface MatchedAttr {
  attr_type: string;
  value: string;
}

/** 推薦商品 1 件（説明付き） */
export interface Recommendation {
  product_id: string;
  title: string;
  display_title: string | null;
  description: string | null;
  image_url: string | null;
  price: number | null;
  avg_rating: number | null;
  rating_count: number | null;
  score: number;
  matched_attrs: MatchedAttr[];
  reason_metrics: ReasonMetrics;
  explanation: string;
  recommendation_source: "dialogue_only" | "dialogue_personalized" | "behavior_only" | "popular";
}

export interface ReasonMetrics {
  condition_matches: number;
  behavior_matches: number;
  transition_peers: number;
  collaborative_peers: number;
  shared_rated_attributes: number;
  shared_viewed_attributes: number;
  review_confirmations: number;
}

/** POST /recommend のレスポンス */
export interface RecommendResponse {
  query: string;
  mode: "search" | "home";
  intent: SearchIntent;
  recommendations: Recommendation[];
  search_id: string;
  fallback: boolean;
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
  translated: boolean;
  display_language: "ja" | "en";
}

export interface ReviewsResponse {
  product_id: string;
  reviews: ReviewItem[];
  requested_language: "ja" | "en";
  translated_count: number;
  fallback_count: number;
}

// ===== 商品説明文 =====
export interface DescriptionResponse {
  product_id: string;
  description: string | null;
  translated: boolean;
}

// ===== 推薦フィードバック =====
export interface FeedbackResponse {
  status: string;
  product_id: string;
}

/** POST /chat のレスポンス */
export interface ChatResponse {
  action: "ask" | "search";
  question: string | null;
  options: string[];
  preference_summary: string[];
  intent: SearchIntent | null;
  recommendations: Recommendation[];
  search_id: string | null;
  fallback: boolean;
  provisional: boolean;
}

// ===== 行動ログ =====
/** POST /behavior/view のリクエスト body */
export interface ViewLogRequest {
  user_id: string;
  product_id: string;
  search_id?: string | null;
}

// ===== デモ用テストユーザー選択 =====
export interface SampleUser {
  user_id: string;
  rated_count: number;
}

export interface SampleUsersResponse {
  users: SampleUser[];
}

// ===== 履歴クリア =====
/** POST /users/{user_id}/clear_history のレスポンス */
export interface ClearHistoryResponse {
  viewed_deleted: number;
  searches_deleted: number;
  feedback_deleted: number;
}
