import { useEffect } from "react";
import type { Recommendation } from "../types/recommend";
import { useI18n } from "../i18n";
import RecommendationCard from "./RecommendationCard";
import { trackBehavior } from "../lib/behavior";
import styles from "./RecommendationList.module.css";

interface Props {
  items: Recommendation[];
  devMode: boolean;
  userId: string;
  query?: string | null;
  source?: string;
}

export default function RecommendationList({ items, devMode, userId, query, source = "chat" }: Props) {
  const { t, lang } = useI18n();

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

  const itemIds = items.map((rec) => rec.product_id).join(",");

  useEffect(() => {
    trackBehavior({
      userId,
      eventType: "impression",
      productIds: items.map((rec) => rec.product_id),
      query,
      source,
      metadata: { count: items.length },
    });
  }, [itemIds, userId, query, source]);

  const heading =
    lang === "ja"
      ? `おすすめ（${items.length}件）`
      : `Recommendations (${items.length})`;

  return (
    <section className={styles.section}>
      <div className={styles.topRow}>
        <h2 className={styles.heading}>{heading}</h2>
      </div>
      <div className={styles.list}>
        {items.map((rec, i) => (
          <RecommendationCard
            key={rec.product_id}
            rec={rec}
            rank={i + 1}
            devMode={devMode}
            userId={userId}
            query={query}
            source={source}
          />
        ))}
      </div>
    </section>
  );
}

