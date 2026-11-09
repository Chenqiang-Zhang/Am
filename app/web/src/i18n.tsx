import { createContext, useContext, useState, type ReactNode } from "react";
import type { Lang } from "./types/recommend";
import type { Answer, Attribute, Relation } from "./types/dialogue";

interface DialogueDict {
  replayNote: string;
  pick: string;
  relationName: Record<Relation | "none", string>;
  opening: (tags: string[], anchor: string | null, relation: Relation | null) => string;
  answers: Record<Answer, string>;
  question: (attribute: Attribute) => string;
  listIntro: (count: number) => string;
  listHit: (rank: number) => string;
  stopped: string;
  passed: string;
  state: string;
  want: string;
  avoid: string;
  budget: string;
  anchor: string;
  none: string;
  priceRange: (min: number | null, max: number | null) => string;
  evidenceTag: string;
  evidenceAnchorTag: (anchor: string) => string;
  evidenceStudio: (anchor: string) => string;
  evidenceCoPlay: (anchor: string) => string;
  fans: (count: number, share: number) => string;
  target: string;
  prev: string;
  next: string;
  turnOf: (turn: number, total: number) => string;
  showAll: (count: number) => string;
  showLess: string;
  simulatedAnswer: string;
  why: {
    title: string;
    summary: (pool: number, yesLabel: string, yes: number, no: number) => string;
    reason: string;
    yes: (attribute: Attribute) => string;
    no: (attribute: Attribute) => string;
    entropy: string;
    alternatives: string;
  };
  card: {
    reasonStudio: (anchor: string, studio: string) => string;
    reasonCoPlay: (anchor: string, count: number, share: number) => string;
    reasonAnchorTags: (anchor: string, tags: string[]) => string;
    reasonTags: (tags: string[]) => string;
    released: (year: number) => string;
    players: (count: number) => string;
    hours: (hours: number) => string;
    positive: (share: number, count: number) => string;
    noReviews: string;
    reviewCount: (count: number) => string;
    without: (label: string) => string;
    more: string;
    details: (reviews: number) => string;
    like: string;
    sameDeveloper: string;
    notThis: string;
    soon: string;
  };
}

interface Dict {
  appTitle: string;
  tabChat: string;
  tabRecommend: string;
  recommendForPlayer: string;
  recommendAnonymous: string;
  greeting: string;
  devView: string;
  anonymous: string;
  player: (index: number, playedCount: number) => string;
  inputPlaceholder: string;
  send: string;
  reset: string;
  thinking: string;
  other: string;
  otherPlaceholder: string;
  empty: string;
  emptyHint: string;
  restartPrompt: string;
  restart: string;
  recommendations: (count: number) => string;
  reasons: string;
  reasonKinds: Record<"tag" | "aspect" | "game" | "studio", string>;
  priceUnknown: string;
  free: string;
  showDescription: string;
  description: string;
  showReviews: string;
  reviews: string;
  loading: string;
  reviewsError: string;
  noReviews: string;
  reviewsShown: (shown: number, total: number) => string;
  reviewsPartial: string;
  recommended: string;
  notRecommended: string;
  played: (hours: number) => string;
  votesUp: (count: number) => string;
  close: string;
  openStore: string;
  tabDialogue: string;
  tabPreview: string;
  dialogue: DialogueDict;
}

const ja: Dict = {
  appTitle: "ゲームコンシェルジュ",
  tabChat: "チャット",
  tabRecommend: "おすすめ",
  recommendForPlayer: "選んだプレイヤーの履歴から、遊んでいないゲームをおすすめします。",
  recommendAnonymous: "履歴がないので、遊んだプレイヤーが多い順に表示しています。右上でプレイヤーを選ぶと、その履歴からおすすめします。",
  greeting: "こんにちは！どんなゲームをお探しですか？",
  devView: "開発者ビュー",
  anonymous: "履歴なし（匿名）",
  player: (index, playedCount) => `プレイヤー ${index}（${playedCount}本プレイ）`,
  inputPlaceholder: "メッセージを入力（例：友達と協力プレイできるアクションゲーム）",
  send: "送信",
  reset: "最初から",
  thinking: "考え中…",
  other: "その他（自由に入力）",
  otherPlaceholder: "自由に入力…",
  empty: "条件に一致するゲームが見つかりませんでした。言葉を変えて、もう一度お試しください。",
  emptyHint: "「最初から」ボタンで会話をリセットできます。",
  restartPrompt: "他のゲームを探しますか？",
  restart: "最初からやり直す",
  recommendations: (count) => `おすすめ（${count}件）`,
  reasons: "根拠",
  reasonKinds: { tag: "タグ", aspect: "観点", game: "遊んだゲーム", studio: "開発元" },
  priceUnknown: "価格未登録",
  free: "無料",
  showDescription: "ゲームの説明を見る",
  description: "ゲームの説明",
  showReviews: "レビューを見る",
  reviews: "レビュー",
  loading: "読み込み中…",
  reviewsError: "レビューを取得できませんでした",
  noReviews: "レビューがありません",
  reviewsShown: (shown, total) => `${shown}件表示 / 取り込み済み${total.toLocaleString()}件`,
  reviewsPartial: "※ 取り込み済みのレビューのうち一部を表示しています",
  recommended: "おすすめする",
  notRecommended: "おすすめしない",
  played: (hours) => `プレイ時間 ${hours.toFixed(1)}時間`,
  votesUp: (count) => `参考になった ${count}件`,
  close: "閉じる",
  openStore: "Steamで見る",
  tabDialogue: "対話",
  tabPreview: "試作",
  dialogue: {
    replayNote:
      "評価用に記録した対話を再生しています。ユーザ役は模擬ユーザ、検索はグラフ（G）です。新しい対話 API につないだら、同じ画面で実際に話せるようにします。",
    pick: "対話を選ぶ",
    relationName: {
      none: "アンカーなし",
      similar: "似ている",
      same_developer: "同じ開発元",
      co_played: "よく遊ぶ人が遊ぶ",
    },
    opening: (tags, anchor, relation) => {
      const want = tags.map((tag) => `「${tag}」`).join("");
      if (!anchor) return `${want}のゲームを探しています。`;
      if (relation === "same_developer") return `${want}のゲームで、${anchor}と同じ開発元のものがいいです。`;
      if (relation === "co_played") return `${want}のゲームで、${anchor}をよく遊ぶ人が遊んでいるものがいいです。`;
      return `${want}のゲームで、${anchor}みたいなものがいいです。`;
    },
    answers: { yes: "はい", no: "いいえ", no_preference: "どちらでもいい" },
    question: (attribute) => {
      if (attribute.kind === "price") return `${attribute.label} 以下がいいですか？`;
      if (attribute.kind === "studio") return `開発元が ${attribute.label} のゲームがいいですか？`;
      return `「${attribute.label}」のゲームがいいですか？`;
    },
    listIntro: (count) => `条件に合いそうなゲームを ${count} 本選びました。`,
    listHit: (rank) => `探していたゲームが ${rank} 番目に入っていました。`,
    stopped: "質問ではこれ以上絞り込めないので、次の候補を見せます。",
    passed: "見送り",
    state: "いまの条件",
    want: "欲しい",
    avoid: "避けたい",
    budget: "予算",
    anchor: "基準のゲーム",
    none: "なし",
    priceRange: (min, max) =>
      min != null && max != null
        ? `$${min} より高く $${max} 以下`
        : max != null
          ? `$${max} 以下`
          : min != null
            ? `$${min} より高い`
            : "指定なし",
    evidenceTag: "タグ",
    evidenceAnchorTag: (anchor) => `${anchor}と共通`,
    evidenceStudio: (anchor) => `${anchor}と同じ開発元`,
    evidenceCoPlay: (anchor) => `${anchor}のファン`,
    fans: (count, share) => `${count.toLocaleString()}人が遊んだ（${(share * 100).toFixed(1)}%）`,
    target: "目標",
    prev: "前へ",
    next: "次へ",
    turnOf: (turn, total) => `ターン ${turn} / ${total}`,
    showAll: (count) => `根拠をすべて見る（${count}）`,
    showLess: "閉じる",
    simulatedAnswer: "模擬ユーザの答え",
    why: {
      title: "なぜこの質問？",
      summary: (pool, yesLabel, yes, no) =>
        `いまの候補 ${pool} 本のうち、${yesLabel}は ${yes} 本、そうでないものは ${no} 本です。`,
      reason: "「はい」でも「いいえ」でも候補がほぼ半分に減る質問を、グラフ上の候補の分かれ方から選んでいます。",
      yes: (a) => (a.kind === "price" ? `${a.label} 以下` : a.kind === "studio" ? `開発元 ${a.label}` : `「${a.label}」`),
      no: (a) => (a.kind === "price" ? `${a.label} より高い` : a.kind === "studio" ? "ほかの開発元" : `「${a.label}」なし`),
      entropy: "分かれ方（1 で半々）",
      alternatives: "次点の質問",
    },
    card: {
      reasonStudio: (anchor, studio) => `${anchor}と同じ ${studio} の作品です。`,
      reasonCoPlay: (anchor, count, share) =>
        `${anchor}をよく遊ぶ人の ${(share * 100).toFixed(1)}%（${count.toLocaleString()} 人）が遊んでいます。`,
      reasonAnchorTags: (anchor, tags) => `${anchor}と${tags.map((tag) => `「${tag}」`).join("")}が共通です。`,
      reasonTags: (tags) => `${tags.map((tag) => `「${tag}」`).join("")}に当てはまります。`,
      released: (year) => `${year}年`,
      players: (count) => `${count.toLocaleString()} 人がプレイ`,
      hours: (hours) => `標準プレイ時間 ${hours} 時間`,
      positive: (share, count) => `好評 ${Math.round(share * 100)}%（${count.toLocaleString()} 件）`,
      noReviews: "レビューなし",
      reviewCount: (count) => `レビュー ${count} 件`,
      without: (label) => `${label}なし`,
      more: "もっと見る",
      details: (reviews) => (reviews > 0 ? `レビュー ${reviews} 件・詳細` : "詳細"),
      like: "これに近いもの",
      sameDeveloper: "同じ開発元で",
      notThis: "これは違う",
      soon: "対話 API につないだら使えます",
    },
  },
};

const en: Dict = {
  appTitle: "Game Concierge",
  tabChat: "Chat",
  tabRecommend: "Recommended",
  recommendForPlayer: "Games this player has not played yet, based on their play history.",
  recommendAnonymous: "No history, so games are shown by number of players. Pick a player at the top right to get recommendations from their history.",
  greeting: "Hi! What kind of game are you looking for?",
  devView: "Developer view",
  anonymous: "No history (anonymous)",
  player: (index, playedCount) => `Player ${index} (${playedCount} games played)`,
  inputPlaceholder: "Type a message (e.g. co-op action game to play with friends)",
  send: "Send",
  reset: "Start over",
  thinking: "Thinking…",
  other: "Other (type your own)",
  otherPlaceholder: "Type here…",
  empty: "No matching games found. Try rephrasing your request.",
  emptyHint: 'Use "Start over" to reset the conversation.',
  restartPrompt: "Looking for something else?",
  restart: "Start over",
  recommendations: (count) => `Recommendations (${count})`,
  reasons: "Reasons",
  reasonKinds: { tag: "Tag", aspect: "Aspect", game: "Played", studio: "Studio" },
  priceUnknown: "No price",
  free: "Free",
  showDescription: "Show description",
  description: "Description",
  showReviews: "Show reviews",
  reviews: "Reviews",
  loading: "Loading…",
  reviewsError: "Could not load reviews",
  noReviews: "No reviews",
  reviewsShown: (shown, total) => `${shown} shown / ${total.toLocaleString()} imported`,
  reviewsPartial: "※ Showing only some of the imported reviews",
  recommended: "Recommended",
  notRecommended: "Not recommended",
  played: (hours) => `${hours.toFixed(1)} h played`,
  votesUp: (count) => `${count} helpful`,
  close: "Close",
  openStore: "Open on Steam",
  tabDialogue: "Dialogue",
  tabPreview: "preview",
  dialogue: {
    replayNote:
      "Replaying a dialogue recorded for evaluation: a simulated user talks to the graph retriever (G). Once the new dialogue API is connected, the same screen will take real input.",
    pick: "Choose a dialogue",
    relationName: {
      none: "No anchor",
      similar: "Similar",
      same_developer: "Same developer",
      co_played: "Played by its fans",
    },
    opening: (tags, anchor, relation) => {
      const want = tags.join(", ");
      if (!anchor) return `I'm looking for a game tagged ${want}.`;
      if (relation === "same_developer") return `A game tagged ${want}, by the same developer as ${anchor}.`;
      if (relation === "co_played") return `A game tagged ${want} that people who play ${anchor} a lot also play.`;
      return `A game tagged ${want}, something like ${anchor}.`;
    },
    answers: { yes: "Yes", no: "No", no_preference: "Doesn't matter" },
    question: (attribute) => {
      if (attribute.kind === "price") return `Should it cost ${attribute.label} or less?`;
      if (attribute.kind === "studio") return `Should it be made by ${attribute.label}?`;
      return `Do you want it to be "${attribute.label}"?`;
    },
    listIntro: (count) => `Here are ${count} games that may fit.`,
    listHit: (rank) => `The game you wanted is number ${rank}.`,
    stopped: "Questions can't narrow it down further, so here are the next games.",
    passed: "Passed",
    state: "Current conditions",
    want: "Want",
    avoid: "Avoid",
    budget: "Budget",
    anchor: "Reference game",
    none: "none",
    priceRange: (min, max) =>
      min != null && max != null
        ? `over $${min}, up to $${max}`
        : max != null
          ? `up to $${max}`
          : min != null
            ? `over $${min}`
            : "any",
    evidenceTag: "Tag",
    evidenceAnchorTag: (anchor) => `Shared with ${anchor}`,
    evidenceStudio: (anchor) => `Same developer as ${anchor}`,
    evidenceCoPlay: (anchor) => `Fans of ${anchor}`,
    fans: (count, share) => `${count.toLocaleString()} played it (${(share * 100).toFixed(1)}%)`,
    target: "Target",
    prev: "Back",
    next: "Next",
    turnOf: (turn, total) => `Turn ${turn} / ${total}`,
    showAll: (count) => `All evidence (${count})`,
    showLess: "Close",
    simulatedAnswer: "Simulated user's answer",
    why: {
      title: "Why this question?",
      summary: (pool, yesLabel, yes, no) =>
        `Of the ${pool} candidates, ${yes} are ${yesLabel} and ${no} are not.`,
      reason: "It picks the question that cuts the candidates about in half whether you say yes or no, from how they split on the graph.",
      yes: (a) => (a.kind === "price" ? `${a.label} or less` : a.kind === "studio" ? `by ${a.label}` : `"${a.label}"`),
      no: (a) => (a.kind === "price" ? `over ${a.label}` : a.kind === "studio" ? "by others" : `not "${a.label}"`),
      entropy: "Split (1 = half and half)",
      alternatives: "Runner-ups",
    },
    card: {
      reasonStudio: (anchor, studio) => `Made by ${studio}, like ${anchor}.`,
      reasonCoPlay: (anchor, count, share) =>
        `${(share * 100).toFixed(1)}% of ${anchor}'s regular players (${count.toLocaleString()}) play it.`,
      reasonAnchorTags: (anchor, tags) => `Shares ${tags.join(", ")} with ${anchor}.`,
      reasonTags: (tags) => `Matches ${tags.join(", ")}.`,
      released: (year) => `${year}`,
      players: (count) => `${count.toLocaleString()} players`,
      hours: (hours) => `typical playtime ${hours} h`,
      positive: (share, count) => `${Math.round(share * 100)}% positive (${count.toLocaleString()})`,
      noReviews: "No reviews",
      reviewCount: (count) => `${count} review${count === 1 ? "" : "s"}`,
      without: (label) => `no ${label}`,
      more: "More",
      details: (reviews) => (reviews > 0 ? `${reviews} review${reviews === 1 ? "" : "s"} · details` : "Details"),
      like: "More like this",
      sameDeveloper: "Same developer",
      notThis: "Not this",
      soon: "Available once the dialogue API is connected",
    },
  },
};

const DICTS: Record<Lang, Dict> = { ja, en };

interface Ctx {
  lang: Lang;
  setLang: (l: Lang) => void;
  t: Dict;
}

const LangContext = createContext<Ctx | null>(null);

export function LangProvider({ children }: { children: ReactNode }) {
  const [lang, setLang] = useState<Lang>("ja");
  return (
    <LangContext.Provider value={{ lang, setLang, t: DICTS[lang] }}>
      {children}
    </LangContext.Provider>
  );
}

export function useI18n(): Ctx {
  const ctx = useContext(LangContext);
  if (!ctx) throw new Error("useI18n must be used within LangProvider");
  return ctx;
}
