import type { Recommendation } from "../types/recommend";
import { useI18n } from "../i18n";
import RecommendationCard from "./RecommendationCard";
import styles from "./RecommendationList.module.css";

interface Props {
  items: Recommendation[];
  devMode: boolean;
}

export default function RecommendationList({ items, devMode }: Props) {
  const { t } = useI18n();

  if (items.length === 0) {
    return (
      <div className={styles.empty}>
        <span className={styles.emptyIcon}>🎮</span>
        <p className={styles.emptyText}>{t.empty}</p>
        <p className={styles.emptyHint}>{t.emptyHint}</p>
      </div>
    );
  }

  return (
    <section className={styles.section}>
      <div className={styles.topRow}>
        <h2 className={styles.heading}>{t.recommendations(items.length)}</h2>
      </div>
      <div className={styles.list}>
        {items.map((rec, i) => (
          <RecommendationCard key={rec.appid} rec={rec} rank={i + 1} devMode={devMode} />
        ))}
      </div>
    </section>
  );
}
