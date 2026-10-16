import { createContext, useContext, useState, type ReactNode } from "react";
import type { Lang } from "./types/recommend";

interface Dict {
  appTitle: string;
  appSubtitle: string;
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
  recommendRate: (rate: number, count: number) => string;
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
}

const ja: Dict = {
  appTitle: "ゲームコンシェルジュ",
  appSubtitle: "会話しながら、あなたにぴったりのゲームをご提案します。",
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
  recommendRate: (rate, count) => `おすすめ率 ${Math.round(rate * 100)}%（レビュー${count.toLocaleString()}件）`,
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
};

const en: Dict = {
  appTitle: "Game Concierge",
  appSubtitle: "We'll suggest games for you through a short chat.",
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
  recommendRate: (rate, count) => `${Math.round(rate * 100)}% recommended (${count.toLocaleString()} reviews)`,
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
