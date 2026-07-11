import { createContext, useContext, useState, type ReactNode } from "react";

// ============================================================================
// 軽量i18n（日本語／英語）。重いライブラリは使わず、文字列辞書＋Contextで実現。
//   UIラベルは t.xxx で参照、言語は useI18n().lang / setLang で切替。
//   ※商品名・説明は元データ（英語）のまま。LLM応答の言語は /chat に lang を渡して制御。
// ============================================================================

export type Lang = "ja" | "en";

interface Dict {
  appTitle: string;
  appSubtitle: string;
  greeting: string;
  devView: string;
  generalMode: string;
  personalizedMode: string;
  personalizedModeDetail: string;
  generalModeDetail: string;
  clearHistory: string;
  clearHistoryConfirm: string;
  clearHistoryDone: string;
  clearHistoryFailed: string;
  inputPlaceholder: string;
  send: string;
  omakase: string;
  reset: string;
  thinking: string;
  other: string;
  otherPlaceholder: string;
  yourPreferences: string;
  recommendationReasons: string;
  fallbackNotice: string;
  empty: string;
  resultFilter: string;
  filterAvailable: string;
  filterAll: string;
  filterUnavailable: string;
  availableLabel: string;
  unavailableLabel: string;
  filteredEmpty: string;
  matchedAttributes: string;
  noAttributeMatch: string;
  intentHeading: string;
  intentWarning: string;
  showCypher: string;
}

const ja: Dict = {
  appTitle: "ゲームコンシェルジュ",
  appSubtitle: "会話しながら、あなたにぴったりのゲームをご提案します。",
  greeting: "こんにちは！どんなゲームをお探しですか？",
  devView: "開発者ビュー",
  generalMode: "一般モード（非個人化）",
  personalizedMode: "個人化推薦",
  personalizedModeDetail: "評価・閲覧履歴をおすすめに反映中",
  generalModeDetail: "履歴を使わない匿名のおすすめ",
  clearHistory: "履歴をクリア",
  clearHistoryConfirm: "このユーザーの閲覧履歴・検索履歴を削除します（評価履歴は削除されません）。よろしいですか？",
  clearHistoryDone: "履歴をクリアしました",
  clearHistoryFailed: "履歴のクリアに失敗しました",
  inputPlaceholder: "メッセージを入力（例：友達と協力プレイできるアクションゲーム）",
  send: "送信",
  omakase: "おまかせで探す",
  reset: "最初から",
  thinking: "考え中…",
  other: "その他（自由に入力）",
  otherPlaceholder: "自由に入力…",
  yourPreferences: "あなたの希望",
  recommendationReasons: "おすすめの理由",
  fallbackNotice: "条件に十分一致する商品が見つからなかったため、人気のゲームも表示しています。",
  empty: "条件に一致する商品が見つかりませんでした。言葉を変えて、もう一度お試しください。",
  resultFilter: "表示",
  filterAvailable: "購入可能",
  filterAll: "すべて",
  filterUnavailable: "販売不可のみ",
  availableLabel: "購入可能",
  unavailableLabel: "売り切れ・現在購入不可",
  filteredEmpty: "この表示条件に一致する商品はありません。",
  matchedAttributes: "一致した属性",
  noAttributeMatch: "構造化属性の一致なし",
  intentHeading: "システムの解釈 (Intent)",
  intentWarning: "検索条件の説明を取得できませんでした。",
  showCypher: "実行されたクエリを見る",
};

const en: Dict = {
  appTitle: "Game Concierge",
  appSubtitle: "We'll suggest games for you through a short chat.",
  greeting: "Hi! What kind of game are you looking for?",
  devView: "Developer view",
  generalMode: "General mode (no personalization)",
  personalizedMode: "Personalized recommendations",
  personalizedModeDetail: "Using your ratings and viewing history",
  generalModeDetail: "Anonymous recommendations without history",
  clearHistory: "Clear history",
  clearHistoryConfirm: "This deletes this user's viewed/search history (rating history is not deleted). Continue?",
  clearHistoryDone: "History cleared",
  clearHistoryFailed: "Failed to clear history",
  inputPlaceholder: "Type a message (e.g. co-op action game to play with friends)",
  send: "Send",
  omakase: "Surprise me",
  reset: "Start over",
  thinking: "Thinking…",
  other: "Other (type your own)",
  otherPlaceholder: "Type here…",
  yourPreferences: "Your preferences",
  recommendationReasons: "Why it fits",
  fallbackNotice: "We couldn't find a close enough match, so we're also showing popular games.",
  empty: "No matching products found. Try rephrasing your request.",
  resultFilter: "View",
  filterAvailable: "Available",
  filterAll: "All",
  filterUnavailable: "Unavailable only",
  availableLabel: "Available",
  unavailableLabel: "Sold out / currently unavailable",
  filteredEmpty: "No products match this result filter.",
  matchedAttributes: "Matched attributes",
  noAttributeMatch: "No structured-attribute match",
  intentHeading: "System interpretation (Intent)",
  intentWarning: "Couldn't retrieve an explanation for the search.",
  showCypher: "Show the executed query",
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
