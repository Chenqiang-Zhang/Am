import { useI18n } from "../i18n";
import styles from "./RatingStars.module.css";

interface Props {
  rating: number | null;
  count: number | null;
}

export default function RatingStars({ rating, count }: Props) {
  const { lang } = useI18n();
  if (rating == null) return null;

  const fillPct = `${Math.min((rating / 5) * 100, 100).toFixed(2)}%`;
  const tip =
    lang === "ja" ? `平均評価 ${rating.toFixed(1)}` : `Average rating ${rating.toFixed(1)}`;

  return (
    <span className={styles.wrapper} title={tip}>
      {/* オーバーレイ方式：全幅を空星で敷き、塗り星を rating 割合でクリップ */}
      <span className={styles.track} aria-hidden="true">
        <span className={styles.empty}>★★★★★</span>
        <span className={styles.fill} style={{ width: fillPct }}>★★★★★</span>
      </span>
      <span className={styles.value}>{rating.toFixed(1)}</span>
      {count != null && <span className={styles.count}>({count.toLocaleString()})</span>}
    </span>
  );
}
