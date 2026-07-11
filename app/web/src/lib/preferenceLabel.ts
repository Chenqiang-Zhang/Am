import type { Lang } from "../i18n";

// 会話LLMがcanonical値（"narrative"等）を返しても、表示上は利用者の言葉に直す。
// 未知の語はそのまま残すため、検索条件そのものを勝手に失わない。
const JA_LABELS: Record<string, string> = {
  mario: "マリオ",
  zelda: "ゼルダ",
  switch: "Nintendo Switch",
  nintendo_switch: "Nintendo Switch",
  game: "ゲームソフト",
  game_mode: "ゲームモード",
  genre: "ジャンル",
  platform: "プラットフォーム",
  action: "アクション",
  adventure: "アドベンチャー",
  rpg: "RPG",
  jrpg: "JRPG",
  puzzle: "パズル",
  shooter: "シューティング",
  racing: "レース",
  sports: "スポーツ",
  simulation: "シミュレーション",
  fighting: "格闘",
  platformer: "プラットフォーマー",
  party: "パーティ",
  cooperative: "協力プレイ",
  co_op: "協力プレイ",
  local_coop: "ローカル協力プレイ",
  online_coop: "オンライン協力プレイ",
  multiplayer: "マルチプレイ",
  single_player: "1人プレイ",
  narrative: "物語性",
  story_driven: "ストーリー重視",
  character_driven: "キャラクター重視",
  pixel_art: "ドット絵",
  retro: "レトロ",
};

export function displayPreference(value: string, lang: Lang): string {
  const trimmed = value.trim();
  if (lang !== "ja" || !trimmed) return trimmed;
  const key = trimmed.toLowerCase().replace(/[\s-]+/g, "_");
  return JA_LABELS[key] ?? trimmed;
}
