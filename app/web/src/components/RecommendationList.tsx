import type { Recommendation } from "../types/recommend";
import { useI18n } from "../i18n";
import RecommendationCard from "./RecommendationCard";
import styles from "./RecommendationList.module.css";

interface Props {
  items: Recommendation[];
  devMode: boolean;
  userId: string | null;
  searchId: string | null;
  fallback: boolean;
}

export default function RecommendationList({ items, devMode, userId, searchId, fallback }: Props) {
  const { t, lang } = useI18n();

  if (items.length === 0) {
    return (
      <div className={styles.empty}>
        <span className={styles.emptyIcon}>🎮</span>
        <p className={styles.emptyText}>{t.empty}</p>
        <p className={styles.emptyHint}>
          {lang === "ja"
            ? "「最初から」ボタンで条件をリセットしてもう一度お試しください。"
            : 'Use "Start over" to reset and try again.'}
        </p>
      </div>
    );
  }

  const heading =
    lang === "ja" ? `おすすめ（${items.length}件）` : `Recommendations (${items.length})`;

  return (
    <section className={styles.section}>
      <div className={styles.topRow}>
        <h2 className={styles.heading}>{heading}</h2>
      </div>
      {fallback && <p className={styles.fallbackNotice}>{t.fallbackNotice}</p>}
      <div className={styles.list}>
        {items.map((rec, i) => (
          <RecommendationCard
            key={rec.product_id}
            rec={rec}
            rank={i + 1}
            devMode={devMode}
            userId={userId}
            searchId={searchId}
          />
        ))}
      </div>
    </section>
  );
}
