import { useI18n, METRIC_LABELS } from "../i18n";
import styles from "./ScoreBreakdown.module.css";

interface Props {
  breakdown: Record<string, number>;
}

// 表示順（recommender.py の score_breakdown に対応）
const ORDER = [
  "attribute_match",
  "feature_text_match",
  "field_match",
  "query_coverage",
  "rating_quality",
  "popularity",
];

function orderedEntries(breakdown: Record<string, number>): [string, number][] {
  const known = ORDER.filter((k) => k in breakdown);
  const unknown = Object.keys(breakdown).filter((k) => !ORDER.includes(k));
  return [...known, ...unknown].map((k) => [k, breakdown[k]]);
}

// 6 指標の水平バー。なぜこの順位かを一目で示す（外部チャートライブラリ不要）。
export default function ScoreBreakdown({ breakdown }: Props) {
  const { lang } = useI18n();
  const labels = METRIC_LABELS[lang];
  const entries = orderedEntries(breakdown);
  if (entries.length === 0) return null;

  return (
    <div className={styles.grid}>
      {entries.map(([key, value]) => {
        const pct = Math.max(0, Math.min(1, value)) * 100;
        return (
          <div key={key} className={styles.row}>
            <span className={styles.label}>{labels[key] ?? key}</span>
            <span className={styles.track}>
              <span className={styles.fill} style={{ width: `${pct}%` }} />
            </span>
            <span className={styles.value}>{value.toFixed(2)}</span>
          </div>
        );
      })}
    </div>
  );
}
