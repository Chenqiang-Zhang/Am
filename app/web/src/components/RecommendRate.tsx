import { useI18n } from "../i18n";
import styles from "./RecommendRate.module.css";

interface Props {
  rate: number | null;
  count: number;
}

export default function RecommendRate({ rate, count }: Props) {
  const { t } = useI18n();
  if (rate == null) return null;
  return <span className={styles.rate}>👍 {t.recommendRate(rate, count)}</span>;
}
