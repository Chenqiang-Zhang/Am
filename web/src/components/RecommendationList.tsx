import { useState } from "react";
import type { Recommendation } from "../types/recommend";
import { useI18n } from "../i18n";
import RecommendationCard from "./RecommendationCard";
import styles from "./RecommendationList.module.css";

interface Props {
  items: Recommendation[];
  devMode: boolean;
}

export default function RecommendationList({ items, devMode }: Props) {
  const { t, lang } = useI18n();
  const [filter, setFilter] = useState<"available" | "all" | "unavailable">("available");

  if (items.length === 0) {
    return (
      <div className={styles.empty}>
        <span className={styles.emptyIcon}>🔍</span>
        <p className={styles.emptyText}>{t.empty}</p>
        <p className={styles.emptyHint}>
          {lang === "ja"
            ? "「最初から」ボタンで条件をリセットしてもう一度お試しください。"
            : 'Use "Start over" to reset and try again.'}
        </p>
      </div>
    );
  }

  const isAvailable = (rec: Recommendation) =>
    rec.availability_status === "available" || rec.price != null;
  const availableCount = items.filter(isAvailable).length;
  const unavailableCount = items.length - availableCount;
  const visibleItems = items.filter((rec) => {
    if (filter === "all") return true;
    if (filter === "available") return isAvailable(rec);
    return !isAvailable(rec);
  });
  const heading =
    lang === "ja"
      ? `おすすめ（${visibleItems.length}/${items.length}件）`
      : `Recommendations (${visibleItems.length}/${items.length})`;

  return (
    <section className={styles.section}>
      <div className={styles.topRow}>
        <h2 className={styles.heading}>{heading}</h2>
        <div className={styles.filterGroup} aria-label={t.resultFilter}>
          <span className={styles.filterLabel}>{t.resultFilter}</span>
          <button
            type="button"
            className={filter === "available" ? styles.filterActive : styles.filterBtn}
            onClick={() => setFilter("available")}
          >
            {t.filterAvailable} ({availableCount})
          </button>
          <button
            type="button"
            className={filter === "all" ? styles.filterActive : styles.filterBtn}
            onClick={() => setFilter("all")}
          >
            {t.filterAll} ({items.length})
          </button>
          <button
            type="button"
            className={filter === "unavailable" ? styles.filterActive : styles.filterBtn}
            onClick={() => setFilter("unavailable")}
          >
            {t.filterUnavailable} ({unavailableCount})
          </button>
        </div>
      </div>
      {visibleItems.length === 0 ? (
        <div className={styles.empty}>
          <p className={styles.emptyText}>{t.filteredEmpty}</p>
          <button type="button" className={styles.emptyAction} onClick={() => setFilter("all")}>
            {t.filterAll}
          </button>
        </div>
      ) : (
        <div className={styles.list}>
          {visibleItems.map((rec, i) => (
            <RecommendationCard key={rec.product_id} rec={rec} rank={i + 1} devMode={devMode} />
          ))}
        </div>
      )}
    </section>
  );
}
