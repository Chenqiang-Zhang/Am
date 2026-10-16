import { useEffect, useState } from "react";
import { recommend, ApiError } from "../api/client";
import type { Recommendation } from "../types/recommend";
import { useI18n } from "../i18n";
import PageHeader, { type PageControls } from "../components/PageHeader";
import RecommendationList from "../components/RecommendationList";
import styles from "./RecommendPage.module.css";

const LIMIT = 10;

export default function RecommendPage(controls: PageControls) {
  const { t, lang } = useI18n();
  const { playerId, devMode } = controls;
  const [recommendations, setRecommendations] = useState<Recommendation[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    recommend({ player_id: playerId || null, limit: LIMIT, lang })
      .then((response) => {
        if (!cancelled) setRecommendations(response.recommendations);
      })
      .catch((e) => {
        if (!cancelled) setError(e instanceof ApiError ? e.message : "Error");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [playerId, lang]);

  return (
    <div className={styles.page}>
      <PageHeader {...controls} />
      <p className={styles.intro}>{playerId ? t.recommendForPlayer : t.recommendAnonymous}</p>
      {loading && <p className={styles.intro}>{t.loading}</p>}
      {error && (
        <div className={styles.error} role="alert">
          {error}
        </div>
      )}
      {!loading && recommendations && <RecommendationList items={recommendations} devMode={devMode} />}
    </div>
  );
}
