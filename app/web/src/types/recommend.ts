export type Lang = "ja" | "en";

export interface Reason {
  kind: "tag" | "aspect" | "game" | "studio";
  label: string;
}

export interface Recommendation {
  appid: string;
  name: string;
  description: string | null;
  image_url: string;
  store_url: string;
  price: number | null;
  currency: "JPY" | "USD" | null;
  tags: string[];
  review_count: number;
  recommend_rate: number | null;
  score: number;
  reasons: Reason[];
  explanation: string;
}

export interface RecommendRequest {
  player_id: string | null;
  limit: number;
  lang: Lang;
}

export interface RecommendResponse {
  recommendations: Recommendation[];
}

export interface ChatMessage {
  role: "user" | "assistant";
  content: string;
}

export interface ChatRequest {
  messages: ChatMessage[];
  player_id: string | null;
  limit: number;
  lang: Lang;
}

export interface ChatResponse {
  reply: string;
  options: string[];
  recommendations: Recommendation[];
}

export interface ReviewItem {
  text: string;
  recommended: boolean | null;
  votes_up: number | null;
  created_at: string | null;
  hours: number | null;
}

export interface ReviewsResponse {
  appid: string;
  reviews: ReviewItem[];
}

export interface SamplePlayer {
  player_id: string;
  played_count: number;
}

export interface SamplePlayersResponse {
  players: SamplePlayer[];
}
