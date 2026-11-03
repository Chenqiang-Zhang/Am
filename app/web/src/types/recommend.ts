export type Lang = "ja" | "en";

export interface Reason {
  kind: "tag" | "aspect" | "game" | "studio";
  label: string;
}

export type EvidencePath = "peer" | "tag" | "studio" | "popular";

export type StudioRole = "developer" | "publisher";

/**
 * One graph path behind a static recommendation. "This game" is the recommended
 * game, the seed is a game the player played, and counts leave the player out.
 * - peer: seed <-PLAYED- `count` other players -PLAYED-> this game
 * - tag: seed -HAS_TAG-> tag `bridge_id` <-HAS_TAG- this game
 * - studio: seed -[seed_role]-> studio `bridge_id` <-[candidate_role]- this game
 *   (`count` = number of games of the studio)
 * - popular: `count` other players -PLAYED-> this game
 * `share` is this path's part of the recommendation's score.
 */
export interface Evidence {
  path: EvidencePath;
  seed_appid: string | null;
  seed_name: string | null;
  bridge_id: string | null;
  bridge_name: string | null;
  count: number | null;
  seed_role: StudioRole | null;
  candidate_role: StudioRole | null;
  share: number;
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
  /** Static recommendations only (null from /chat): evidence, headline first. */
  evidence?: Evidence[] | null;
  /** Static recommendations only: the score split by path; the values add up to `score`. */
  contributions?: Partial<Record<EvidencePath, number>> | null;
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
