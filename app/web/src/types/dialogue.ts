// The dialogue recommender's turn, as the screen expects it. For now it is filled from recorded E1-pre
// dialogues (src/mocks/dialogues.json, built by scripts/build_dialogue_mock.py); the dialogue API is meant to
// return the same shape. Field names follow app/api/chat_state.py (ChatState, ChatEvidence).

export type AttributeKind = "tag" | "studio" | "price";

export interface Attribute {
  key: string; // "tag:co_op", "studio:valve", "price:10"
  kind: AttributeKind;
  label: string; // display name: tag or studio name, or "$10"
}

export interface GameRef {
  appid: string;
  name: string;
}

export type Relation = "similar" | "same_developer" | "co_played";

export type Answer = "yes" | "no" | "no_preference";

export interface DialogueState {
  include: Attribute[];
  exclude: Attribute[];
  anchors: GameRef[];
  relation: Relation | null;
  max_price: number | null;
  min_price: number | null; // more than, from a "no" to a price band
}

export type EvidencePath = "tag" | "anchor_tag" | "studio" | "co_play";

export interface Evidence {
  path: EvidencePath;
  label: string; // the tag or studio on the path; empty for co_play
  share: number; // tag: strength 0.1-1; anchor_tag/studio: IDF or studio strength; co_play: fans who played it
  anchor?: GameRef;
  count?: number; // co_play: how many of the anchor's fans played it
}

// One condition of the state and whether the game meets it.
export type ConditionCheck =
  | { kind: "include" | "exclude"; attribute: Attribute; ok: boolean }
  | { kind: "budget"; min: number | null; max: number | null; ok: boolean };

// Facts about the game from the graph, besides the evidence for this recommendation.
export interface GameInfo {
  developers: string[];
  year: number | null;
  players: number; // UCSD players who played it
  median_hours: number | null;
  reviews: number; // reviews with a thumbs up or down
  positive: number | null; // share of thumbs up, 0-1
  readable_reviews: number; // reviews GET /games/{appid}/reviews can show (text over 10 characters)
  top_tags: string[]; // its user tags of rank 1-5
}

export interface DialogueCandidate {
  appid: string;
  name: string;
  image_url: string;
  store_url: string;
  price: number | null;
  score: number;
  is_target: boolean; // recorded dialogues only: the hidden game the simulated user wants
  evidence: Evidence[];
  checks: ConditionCheck[];
  info: GameInfo | null;
}

export interface UserTurn {
  include: Attribute[];
  exclude: Attribute[];
  anchors: GameRef[];
  relation: Relation | null;
  answer?: { question: Attribute; answer: Answer | null }; // the reply to the previous turn's question
}

// Why the questioner picked this attribute: it splits the candidate pool most evenly (highest entropy,
// weighted by the retrieval scores), so either answer removes about half of the pool.
export interface QuestionWhy {
  pool: number; // candidates the questioner looked at
  yes: number; // of them, games that have the attribute (a "yes" keeps these)
  no: number;
  share: number; // weighted share of "yes" games, 0-1
  entropy: number; // 0-1; 1 is an even split
  alternatives: { attribute: Attribute; entropy: number }[]; // the runner-ups
}

export interface DialogueTurn {
  turn: number;
  user: UserTurn;
  state: DialogueState; // the state the games were retrieved for
  candidates: DialogueCandidate[];
  question: Attribute | null; // asked after the list; null when the system stopped asking or it hit
  answer: Answer | null;
  why: QuestionWhy | null;
  hit_rank: number | null;
}

export interface RecordedDialogue {
  id: string;
  run: string;
  relation: Relation | null;
  anchor: GameRef | null;
  target: GameRef;
  turns: DialogueTurn[];
}
