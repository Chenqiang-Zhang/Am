import type { useI18n } from "../i18n";
import type { CardChip, ChipTone, GameCardData } from "../types/card";
import type { ConditionCheck, DialogueCandidate, Evidence } from "../types/dialogue";
import type { Recommendation } from "../types/recommend";

type Dict = ReturnType<typeof useI18n>["t"];

const REASON_TONES: Record<Recommendation["reasons"][number]["kind"], ChipTone> = {
  tag: "plain",
  aspect: "aspect",
  game: "game",
  studio: "studio",
};

// The static recommender (chat and recommendation tabs). Its reviews API applies the same filter, so its
// review_count is what the reviews panel can show.
export function cardFromRecommendation(rec: Recommendation, t: Dict): GameCardData {
  return {
    appid: rec.appid,
    name: rec.name,
    image_url: rec.image_url,
    store_url: rec.store_url,
    price: rec.price,
    currency: rec.currency,
    meta: [],
    checks: [],
    reason: rec.explanation || null,
    chips: rec.reasons.map((r) => ({ kind: t.reasonKinds[r.kind], value: r.label, tone: REASON_TONES[r.kind] })),
    stats: rec.review_count > 0 ? { reviews: rec.review_count, positive: rec.recommend_rate } : null,
    tags: rec.tags,
    readableReviews: rec.review_count,
    description: rec.description,
    target: false,
    devNote: `${rec.appid} · ${rec.score.toFixed(2)}`,
  };
}

function strongest(evidence: Evidence[], path: Evidence["path"], count: number): Evidence[] {
  return evidence.filter((item) => item.path === path).sort((a, b) => b.share - a.share).slice(0, count);
}

// One sentence from the strongest graph path: developer, then co-play, then tags shared with the anchor.
// Without a graph path there is no sentence: the checks already say which asked-for tags the game has.
// Also returns the evidence the sentence says, so the chips do not repeat it.
function graphReason(evidence: Evidence[], t: Dict): [string | null, Evidence[]] {
  const c = t.dialogue.card;
  const [studio] = strongest(evidence, "studio", 1);
  if (studio) return [c.reasonStudio(studio.anchor?.name ?? "", studio.label), [studio]];
  const [coPlay] = strongest(evidence, "co_play", 1);
  if (coPlay) return [c.reasonCoPlay(coPlay.anchor?.name ?? "", coPlay.count ?? 0, coPlay.share), [coPlay]];
  const shared = strongest(evidence, "anchor_tag", 2);
  if (shared.length > 0) {
    return [c.reasonAnchorTags(shared[0].anchor?.name ?? "", shared.map((item) => item.label)), shared];
  }
  return [null, []];
}

function evidenceChip(item: Evidence, t: Dict): CardChip {
  const d = t.dialogue;
  const anchor = item.anchor?.name ?? "";
  switch (item.path) {
    case "studio":
      return { kind: d.evidenceStudio(anchor), value: item.label, tone: "studio", weight: item.share };
    case "co_play":
      return { kind: d.evidenceCoPlay(anchor), value: d.fans(item.count ?? 0, item.share), tone: "coplay" };
    case "anchor_tag":
      return { kind: d.evidenceAnchorTag(anchor), value: item.label, tone: "anchor", weight: item.share };
    default:
      return { kind: d.evidenceTag, value: item.label, tone: "plain", weight: item.share };
  }
}

const CHIP_ORDER: Record<Evidence["path"], number> = { studio: 0, co_play: 1, anchor_tag: 2, tag: 3 };

function checkLabel(check: ConditionCheck, t: Dict): string {
  if (check.kind === "budget") return t.dialogue.priceRange(check.min, check.max);
  if (check.kind === "exclude") return t.dialogue.card.without(check.attribute.label);
  return check.attribute.label;
}

// The dialogue recommender. The user's own tags show as checks, so the chips keep the other graph paths
// (all of the evidence in the developer view).
export function cardFromCandidate(game: DialogueCandidate, rank: number, t: Dict, devMode: boolean): GameCardData {
  const info = game.info;
  const [reason, said] = graphReason(game.evidence, t);
  const chips = [...game.evidence]
    .filter((item) => devMode || (item.path !== "tag" && !said.includes(item)))
    .sort((a, b) => CHIP_ORDER[a.path] - CHIP_ORDER[b.path] || b.share - a.share)
    .map((item) => evidenceChip(item, t));
  return {
    appid: game.appid,
    name: game.name,
    image_url: game.image_url,
    store_url: game.store_url,
    price: game.price,
    currency: "USD",
    meta: info ? [...info.developers, ...(info.year ? [t.dialogue.card.released(info.year)] : [])] : [],
    checks: game.checks.map((check) => ({ label: checkLabel(check, t), ok: check.ok })),
    reason,
    chips,
    stats: info
      ? { players: info.players, hours: info.median_hours, reviews: info.reviews, positive: info.positive }
      : null,
    tags: info?.top_tags ?? [],
    readableReviews: info?.readable_reviews ?? 0,
    description: null,
    target: game.is_target,
    devNote: `#${rank} · ${game.appid} · ${game.score.toFixed(2)}`,
  };
}
