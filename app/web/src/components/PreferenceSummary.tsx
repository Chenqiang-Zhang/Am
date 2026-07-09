import { useI18n } from "../i18n";
import styles from "./PreferenceSummary.module.css";

interface Props {
  items: string[];
}

// 確定した「あなたの希望」を表示（説明可能性：何を条件に選んだかを明示）。
export default function PreferenceSummary({ items }: Props) {
  const { t } = useI18n();
  if (items.length === 0) return null;
  return (
    <div className={styles.wrap}>
      <span className={styles.label}>{t.yourPreferences}</span>
      <div className={styles.chips}>
        {items.map((i) => (
          <span key={i} className={styles.chip}>
            {i}
          </span>
        ))}
      </div>
    </div>
  );
}
