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
  inputPlaceholder: string;
  send: string;
  omakase: string;
  reset: string;
  thinking: string;
  other: string;
  otherPlaceholder: string;
  yourPreferences: string;
  empty: string;
  matchedAttributes: string;
  matchedTerms: string;
  evidence: string;
  scoreBreakdown: string;
  noAttributeMatch: string;
  noTermMatch: string;
  intentHeading: string;
  intentWarning: string;
  attrFilters: string;
  keywords: string;
  maxPrice: string;
  minRating: string;
  none: string;
  notSet: string;
}

const ja: Dict = {
  appTitle: "商品コンシェルジュ",
  appSubtitle: "会話しながら、あなたに合う商品をご提案します。",
  greeting: "こんにちは！どんな商品をお探しですか？",
  devView: "開発者ビュー",
  inputPlaceholder: "メッセージを入力（例：乾燥肌向けの保湿クリーム）",
  send: "送信",
  omakase: "おまかせで探す",
  reset: "最初から",
  thinking: "考え中…",
  other: "その他（自由に入力）",
  otherPlaceholder: "自由に入力…",
  yourPreferences: "あなたの希望",
  empty: "条件に一致する商品が見つかりませんでした。言葉を変えて、もう一度お試しください。",
  matchedAttributes: "一致した属性",
  matchedTerms: "一致テキスト",
  evidence: "根拠（特徴文）",
  scoreBreakdown: "スコア内訳",
  noAttributeMatch: "構造化属性の一致なし（テキスト一致のみ）",
  noTermMatch: "一致テキストなし",
  intentHeading: "システムの解釈 (Intent)",
  intentWarning: "クエリから検索条件を抽出できませんでした。表現を変えて再検索してみてください。",
  attrFilters: "属性フィルタ",
  keywords: "キーワード",
  maxPrice: "価格上限",
  minRating: "最低評価",
  none: "なし",
  notSet: "指定なし",
};

const en: Dict = {
  appTitle: "Product Concierge",
  appSubtitle: "We'll suggest products for you through a short chat.",
  greeting: "Hi! What kind of product are you looking for?",
  devView: "Developer view",
  inputPlaceholder: "Type a message (e.g. moisturizer for dry skin)",
  send: "Send",
  omakase: "Surprise me",
  reset: "Start over",
  thinking: "Thinking…",
  other: "Other (type your own)",
  otherPlaceholder: "Type here…",
  yourPreferences: "Your preferences",
  empty: "No matching products found. Try rephrasing your request.",
  matchedAttributes: "Matched attributes",
  matchedTerms: "Matched terms",
  evidence: "Evidence (from description)",
  scoreBreakdown: "Score breakdown",
  noAttributeMatch: "No structured-attribute match (text match only)",
  noTermMatch: "No matched terms",
  intentHeading: "System interpretation (Intent)",
  intentWarning: "Couldn't extract search criteria from the query. Try rephrasing.",
  attrFilters: "Attribute filters",
  keywords: "Keywords",
  maxPrice: "Max price",
  minRating: "Min rating",
  none: "none",
  notSet: "not set",
};

const DICTS: Record<Lang, Dict> = { ja, en };

// ScoreBreakdown の指標ラベル（言語別）
export const METRIC_LABELS: Record<Lang, Record<string, string>> = {
  ja: {
    attribute_match: "属性一致",
    feature_text_match: "特徴テキスト一致",
    field_match: "フィールド一致",
    query_coverage: "クエリ被覆率",
    rating_quality: "評価品質",
    popularity: "人気度",
  },
  en: {
    attribute_match: "Attribute match",
    feature_text_match: "Feature-text match",
    field_match: "Field match",
    query_coverage: "Query coverage",
    rating_quality: "Rating quality",
    popularity: "Popularity",
  },
};

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
