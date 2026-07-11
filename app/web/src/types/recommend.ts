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

/** クエリの「システムによる解釈」（Text2Cypher: LLMが生成したCypherとその一文説明） */
export interface SearchIntent {
  cypher: string;
  cypher_explanation: string;
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
  image_url: string | null;
  price: number | null;
  avg_rating: number | null;
  rating_count: number | null;
  score: number;
  matched_attrs: MatchedAttr[];
  explanation: string;
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
}

export interface ReviewsResponse {
  product_id: string;
  reviews: ReviewItem[];
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
