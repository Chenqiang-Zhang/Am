// ============================================================================
// 機械的なマッチ情報 → ユーザーに優しい表現に変換するヘルパー。
//   日本語モードは語を日本語ラベルに変換、英語モードは原語（英語）のまま。
// ============================================================================
import type { Recommendation } from "../types/recommend";
import type { Lang } from "../i18n";

// よく出る美容系の語を日本語ラベルへ（日本語モード用）。
const TERM_LABELS: Record<string, string> = {
  dry: "乾燥肌",
  oily: "脂性肌",
  sensitive: "敏感肌",
  combination: "混合肌",
  normal: "普通肌",
  acne: "ニキビ肌",
  "acne-prone": "ニキビ肌",
  "fragrance-free": "無香料",
  "fragrance free": "無香料",
  unscented: "無香料",
  floral: "フローラルな香り",
  citrus: "柑橘系の香り",
  fresh: "爽やかな香り",
  moisturizing: "保湿",
  moisturizer: "保湿",
  hydrating: "保湿",
  nourishing: "うるおい補給",
  soothing: "低刺激・鎮静",
  gentle: "低刺激",
  brightening: "明るさ・くすみケア",
  "anti-aging": "エイジングケア",
  "anti aging": "エイジングケア",
  repair: "補修",
  "hyaluronic acid": "ヒアルロン酸",
  "vitamin c": "ビタミンC",
  "vitamin e": "ビタミンE",
  retinol: "レチノール",
  niacinamide: "ナイアシンアミド",
  "aloe vera": "アロエ",
  "shea butter": "シアバター",
  collagen: "コラーゲン",
  serum: "美容液",
  cleanser: "洗顔",
  toner: "化粧水",
  lotion: "乳液",
  cream: "クリーム",
  sunscreen: "日焼け止め",
  spf: "UVカット",
  mask: "マスク",
  shampoo: "シャンプー",
  face: "顔用",
  facial: "顔用",
  eye: "目元用",
  lip: "リップ",
  body: "ボディ用",
  hair: "ヘア用",
};

function labelFor(term: string, lang: Lang): string {
  const titled = term.charAt(0).toUpperCase() + term.slice(1);
  if (lang === "en") return titled; // 英語モードは原語のまま
  const key = term.toLowerCase().trim();
  return TERM_LABELS[key] ?? titled;
}

/** 「おすすめポイント」タグ（言語に応じたラベル・重複排除・上限あり） */
export function friendlyTags(rec: Recommendation, lang: Lang, max = 6): string[] {
  const raw = [
    ...rec.matched_attributes.map((a) => a.value),
    ...rec.matched_terms,
  ];
  const seen = new Set<string>();
  const out: string[] = [];
  for (const term of raw) {
    if (!term) continue;
    const label = labelFor(term, lang);
    if (seen.has(label)) continue;
    seen.add(label);
    out.push(label);
    if (out.length >= max) break;
  }
  return out;
}

/** 商品説明からの根拠（実テキスト）。あれば1件を120文字で返す。 */
export function evidenceQuote(rec: Recommendation, maxLen = 120): string | null {
  const text = rec.matched_feature_evidence[0] ?? null;
  if (!text) return null;
  return text.length <= maxLen ? text : text.slice(0, maxLen).trimEnd() + "…";
}
