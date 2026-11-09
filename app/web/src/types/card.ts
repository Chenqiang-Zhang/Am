// What a game card shows, the same for every tab. The chat and recommendation tabs fill it from the
// static recommender's Recommendation, the dialogue tab from a DialogueCandidate (components/cardData.ts).

// The colour of a reason chip: graph paths through an anchor stand out from plain tags.
export type ChipTone = "plain" | "anchor" | "studio" | "coplay" | "aspect" | "game";

export interface CardChip {
  kind: string; // what kind of reason, e.g. "タグ", "Portal 2と共通"
  value: string;
  tone: ChipTone;
  weight?: number; // shown in the developer view
}

export interface CardCheck {
  label: string;
  ok: boolean;
}

export interface CardStats {
  players?: number;
  hours?: number | null; // typical playtime: the median of its players' hours
  reviews?: number; // rated reviews
  positive?: number | null; // share of thumbs up, 0-1
}

export interface GameCardData {
  appid: string;
  name: string;
  image_url: string;
  store_url: string;
  price: number | null;
  currency: "USD" | "JPY" | null;
  meta: string[]; // developer, year
  checks: CardCheck[]; // the user's conditions and whether the game meets them
  reason: string | null; // one sentence
  chips: CardChip[];
  stats: CardStats | null;
  tags: string[];
  readableReviews: number; // how many reviews the reviews API can show
  description: string | null;
  target: boolean; // recorded dialogues: the game the simulated user wanted
  devNote: string | null; // appid and score, for the developer view
}
