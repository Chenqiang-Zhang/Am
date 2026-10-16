import { useState } from "react";
import type { Recommendation } from "../types/recommend";
import { useI18n } from "../i18n";
import ReviewList from "./ReviewList";
import ReasonChips from "./ReasonChips";
import styles from "./RecommendationCard.module.css";

interface Props {
  rec: Recommendation;
  rank: number;
  devMode: boolean;
}

function formatPrice(rec: Recommendation, free: string, unknown: string): string {
  if (rec.price == null) return unknown;
  if (rec.price === 0) return free;
  if (rec.currency === "USD") return `$${rec.price.toFixed(2)}`;
  return `¥${Math.round(rec.price).toLocaleString("ja-JP")}`;
}

export default function RecommendationCard({ rec, rank, devMode }: Props) {
  const { t } = useI18n();

  return (
    <article className={styles.card} style={{ animationDelay: `${(rank - 1) * 70}ms` }}>
      <a href={rec.store_url} target="_blank" rel="noreferrer" title={t.openStore}>
        <GameImage src={rec.image_url} alt={rec.name} />
      </a>
      <div className={styles.body}>
        <div className={styles.head}>
          {devMode && <span className={styles.rank}>#{rank}</span>}
          <h3 className={styles.title} title={rec.name}>
            <a className={styles.titleLink} href={rec.store_url} target="_blank" rel="noreferrer">
              {rec.name}
            </a>
          </h3>
        </div>

        <div className={styles.meta}>
          <span className={rec.price != null ? styles.price : styles.priceUnknown}>
            {formatPrice(rec, t.free, t.priceUnknown)}
          </span>
          {devMode && <span className={styles.id}>{rec.appid}</span>}
        </div>

        {devMode ? (
          <div className={styles.dev}>
            {rec.explanation && <p className={styles.explanation}>{rec.explanation}</p>}
            <div className={styles.section}>
              <span className={styles.sectionLabel}>{t.reasons}</span>
              <div className={styles.sectionBody}>
                <ReasonChips reasons={rec.reasons} />
              </div>
            </div>
          </div>
        ) : (
          <div className={styles.user}>
            {rec.explanation && <p className={styles.explanation}>{rec.explanation}</p>}
            {rec.tags.length > 0 && (
              <div className={styles.userTags}>
                {rec.tags.map((tag) => (
                  <span key={tag} className={styles.userTag}>
                    {tag}
                  </span>
                ))}
              </div>
            )}
          </div>
        )}
        {rec.description && <ExpandableDescription text={rec.description} />}
        <ReviewList appid={rec.appid} reviewCount={rec.review_count} />
      </div>
    </article>
  );
}

function ExpandableDescription({ text }: { text: string }) {
  const { t } = useI18n();
  const [open, setOpen] = useState(false);

  if (!open) {
    return (
      <button
        type="button"
        className={styles.descriptionTrigger}
        onClick={() => setOpen(true)}
        aria-expanded={false}
      >
        <span className={styles.descriptionTriggerIcon} aria-hidden="true">ⓘ</span>
        <span>{t.showDescription}</span>
        <span className={styles.descriptionChevron} aria-hidden="true">›</span>
      </button>
    );
  }

  return (
    <div className={styles.descriptionBlock}>
      <div className={styles.descriptionHeader}>
        <span className={styles.descriptionHeading}>
          <span className={styles.descriptionTriggerIcon} aria-hidden="true">ⓘ</span>
          {t.description}
        </span>
        <button
          type="button"
          className={styles.descriptionCloseBtn}
          onClick={() => setOpen(false)}
          aria-expanded={true}
        >
          {t.close} <span aria-hidden="true">×</span>
        </button>
      </div>
      <p className={styles.descriptionText}>{text}</p>
    </div>
  );
}

function GameImage({ src, alt }: { src: string; alt: string }) {
  const [failed, setFailed] = useState(false);
  if (failed) {
    return (
      <div className={styles.thumbPlaceholder} aria-hidden="true">
        <svg className={styles.thumbIcon} viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
          <path
            d="M7 9c-2.5 0-4.3 2-3.9 4.4l.6 3.4c.3 1.6 2.1 2.4 3.5 1.5l.7-.4a3.5 3.5 0 0 1 3.3 0l.2.1a3.5 3.5 0 0 0 3.3 0l.2-.1a3.5 3.5 0 0 1 3.3 0l.7.4c1.4.9 3.2.1 3.5-1.5l.6-3.4C23.3 11 21.5 9 19 9z"
            stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round"
          />
          <path d="M8.5 12.5v3M7 14h3" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"/>
          <circle cx="16" cy="12" r="0.9" fill="currentColor"/>
          <circle cx="18.2" cy="14.2" r="0.9" fill="currentColor"/>
        </svg>
      </div>
    );
  }
  return (
    <img
      className={styles.thumb}
      src={src}
      alt={alt}
      loading="lazy"
      onError={() => setFailed(true)}
    />
  );
}
