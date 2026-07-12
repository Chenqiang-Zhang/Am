import type { Lang } from "../i18n";

// 会話LLMがcanonical値（"narrative"等）や "franchise: mario" のような
// key:value形式を返しても、表示上は利用者の言葉に直す。
// 未知の語はスネークケース整形にフォールバックし、検索条件そのものを失わない。
const JA_LABELS: Record<string, string> = {
  mario: "マリオ",
  zelda: "ゼルダ",
  pokemon: "ポケモン",
  kirby: "カービィ",
  splatoon: "スプラトゥーン",
  animal_crossing: "どうぶつの森",
  minecraft: "マインクラフト",
  final_fantasy: "ファイナルファンタジー",
  switch: "Nintendo Switch",
  nintendo_switch: "Nintendo Switch",
  nintendo_3ds: "Nintendo 3DS",
  nintendo_ds: "Nintendo DS",
  wii_u: "Wii U",
  wii: "Wii",
  ps5: "PlayStation 5",
  ps4: "PlayStation 4",
  ps3: "PlayStation 3",
  xbox_one: "Xbox One",
  xbox_360: "Xbox 360",
  xbox_series_x: "Xbox Series X|S",
  pc: "PC",
  game: "ゲームソフト",
  video_game: "ゲームソフト",
  controller: "コントローラー",
  console: "ゲーム機本体",
  headset: "ヘッドセット",
  accessory: "アクセサリ",
  action: "アクション",
  adventure: "アドベンチャー",
  action_adventure: "アクションアドベンチャー",
  rpg: "RPG",
  action_rpg: "アクションRPG",
  jrpg: "JRPG",
  puzzle: "パズル",
  shooter: "シューティング",
  racing: "レース",
  sports: "スポーツ",
  simulation: "シミュレーション",
  fighting: "格闘",
  horror: "ホラー",
  platformer: "プラットフォーマー",
  party: "パーティ",
  open_world: "オープンワールド",
  cooperative: "協力プレイ",
  co_op: "協力プレイ",
  coop: "協力プレイ",
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

// key:value形式で来た場合のkey側（"franchise: mario"の"franchise"）の訳。
// チップにはvalueを主として表示し、keyは括弧で補足する。
const JA_KEY_LABELS: Record<string, string> = {
  franchise: "シリーズ",
  platform: "機種",
  game_mode: "遊び方",
  play_mode: "遊び方",
  genre: "ジャンル",
  product_type: "商品タイプ",
  product: "商品",
  category: "カテゴリ",
  attribute: "特徴",
  price: "価格",
  min_rating: "最低評価",
};

const ACRONYMS = new Set(["rpg", "fps", "3ds", "ds", "pc", "hd", "4k", "vr", "dlc", "2d", "3d"]);

function normalizeKey(value: string): string {
  return value.trim().toLowerCase().replace(/[\s-]+/g, "_");
}

// 未知のsnake_case値を "action_rpg"→"Action RPG" 風に整形する
function prettify(value: string): string {
  return value
    .split(/[\s_]+/)
    .filter(Boolean)
    .map((w) => (ACRONYMS.has(w.toLowerCase()) ? w.toUpperCase() : w.charAt(0).toUpperCase() + w.slice(1)))
    .join(" ");
}

function displayValue(raw: string, lang: Lang): string {
  const key = normalizeKey(raw);
  if (lang === "ja" && JA_LABELS[key]) return JA_LABELS[key];
  // 日英とも、機械的なcanonical値（snake_case/全小文字）は読める形に整形する
  if (/_/.test(raw) || raw === raw.toLowerCase()) return prettify(raw);
  return raw.trim();
}

export function displayPreference(value: string, lang: Lang): string {
  const trimmed = value.trim();
  if (!trimmed) return trimmed;

  // "franchise: mario" のような key:value 形式をパースして両側を訳す
  const kv = trimmed.match(/^([a-z][a-z0-9_ -]{1,24}):\s*(.+)$/i);
  if (kv) {
    const keyLabel = lang === "ja" ? JA_KEY_LABELS[normalizeKey(kv[1])] : undefined;
    const valueLabel = displayValue(kv[2], lang);
    return keyLabel ? `${valueLabel}（${keyLabel}）` : valueLabel;
  }

  if (lang !== "ja") return trimmed;
  return displayValue(trimmed, lang);
}
