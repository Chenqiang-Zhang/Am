import { useState } from "react";
import type { CardStats, GameCardData } from "../types/card";
import { useI18n } from "../i18n";
import GameDrawer from "./GameDrawer";
import styles from "./GameCard.module.css";

interface Props {
  data: GameCardData;
  rank: number;
  devMode: boolean;
  actions?: boolean; // the dialogue's next-step buttons (disabled until the dialogue API is connected)
}

const CHIPS_SHOWN = 3;
// Below this many rated reviews a share of thumbs up says little, so only the count is shown.
const MIN_REVIEWS_FOR_SHARE = 5;

export function formatPrice(price: number | null, currency: GameCardData["currency"], free: string,
                            unknown: string): string {
  if (price == null) return unknown;
  if (price === 0) return free;
  if (currency === "JPY") return `¥${Math.round(price).toLocaleString("ja-JP")}`;
  return `$${price.toFixed(2)}`;
}

export function StatsLine({ stats }: { stats: CardStats }) {
  const { t } = useI18n();
  const c = t.dialogue.card;
  const reviews = stats.reviews ?? 0;
  return (
    <div className={styles.stats}>
      {stats.players != null && <span>👥 {c.players(stats.players)}</span>}
      {stats.hours != null && <span>⏱ {c.hours(stats.hours)}</span>}
      {stats.reviews != null && (
        <span>
          {reviews === 0
            ? c.noReviews
            : stats.positive != null && reviews >= MIN_REVIEWS_FOR_SHARE
              ? `👍 ${c.positive(stats.positive, reviews)}`
              : `💬 ${c.reviewCount(reviews)}`}
        </span>
      )}
    </div>
  );
}

export default function GameCard({ data, rank, devMode, actions = false }: Props) {
  const { t } = useI18n();
  const c = t.dialogue.card;
  const [imageFailed, setImageFailed] = useState(false);
  const [allChips, setAllChips] = useState(false);
  const [drawer, setDrawer] = useState(false);
  const highlight = devMode && data.target;
  const chips = allChips ? data.chips : data.chips.slice(0, CHIPS_SHOWN);

  return (
    <article className={highlight ? styles.target : styles.card} style={{ animationDelay: `${(rank - 1) * 40}ms` }}>
      <a className={styles.thumbLink} href={data.store_url} target="_blank" rel="noreferrer" title={t.openStore}>
        {imageFailed ? (
          <div className={styles.thumbPlaceholder} aria-hidden="true" />
        ) : (
          <img className={styles.thumb} src={data.image_url} alt={data.name} loading="lazy"
               onError={() => setImageFailed(true)} />
        )}
        <span className={styles.rank}>{rank}</span>
        {highlight && <span className={styles.targetBadge}>{t.dialogue.target}</span>}
        <span className={styles.price}>{formatPrice(data.price, data.currency, t.free, t.priceUnknown)}</span>
      </a>

      <div className={styles.body}>
        <a className={styles.title} href={data.store_url} target="_blank" rel="noreferrer" title={data.name}>
          {data.name}
        </a>
        {(data.meta.length > 0 || (devMode && data.devNote)) && (
          <div className={styles.meta}>
            {data.meta.map((item) => <span key={item}>{item}</span>)}
            {devMode && data.devNote && <span className={styles.dev}>{data.devNote}</span>}
          </div>
        )}

        {data.checks.length > 0 && (
          <ul className={styles.checks}>
            {data.checks.map((check, i) => (
              <li key={i} className={check.ok ? styles.ok : styles.ng}>
                <span aria-hidden="true">{check.ok ? "✓" : "✗"}</span>
                {check.label}
              </li>
            ))}
          </ul>
        )}

        {data.reason && (
          <p className={styles.reason}>
            <span aria-hidden="true">💡</span>
            {data.reason}
          </p>
        )}

        {data.chips.length > 0 && (
          <div className={styles.chips}>
            {chips.map((chip, i) => (
              <span key={`${chip.kind}-${chip.value}-${i}`} className={styles[`chip_${chip.tone}`]}>
                <span className={styles.chipKind}>{chip.kind}</span>
                <span>{chip.value}</span>
                {devMode && chip.weight != null && <span className={styles.chipWeight}>{chip.weight.toFixed(2)}</span>}
              </span>
            ))}
            {data.chips.length > CHIPS_SHOWN && (
              <button type="button" className={styles.moreChips} onClick={() => setAllChips(!allChips)}>
                {allChips ? t.dialogue.showLess : t.dialogue.showAll(data.chips.length)}
              </button>
            )}
          </div>
        )}

        {data.stats && <StatsLine stats={data.stats} />}

        {data.tags.length > 0 && (
          <div className={styles.tags}>
            {data.tags.map((tag) => <span key={tag}>{tag}</span>)}
          </div>
        )}

        <div className={styles.footer}>
          <button type="button" className={styles.details} onClick={() => setDrawer(true)}>
            <span aria-hidden="true">★</span>
            {c.details(data.readableReviews)}
            <span aria-hidden="true">›</span>
          </button>
          {actions && (
            <div className={styles.actions} title={c.soon}>
              <button type="button" disabled>{c.like}</button>
              <button type="button" disabled>{c.sameDeveloper}</button>
              <button type="button" disabled>{c.notThis}</button>
            </div>
          )}
        </div>
      </div>

      {drawer && <GameDrawer data={data} onClose={() => setDrawer(false)} />}
    </article>
  );
}
