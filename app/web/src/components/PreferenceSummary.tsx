import { useI18n } from "../i18n";
import { displayPreference } from "../lib/preferenceLabel";
import styles from "./PreferenceSummary.module.css";

interface Props {
  items: string[];
}

// 確定した「あなたの希望」を表示（説明可能性：何を条件に選んだかを明示）。
export default function PreferenceSummary({ items }: Props) {
  const { t, lang } = useI18n();
  if (items.length === 0) return null;
  return (
    <div className={styles.wrap}>
      <span className={styles.label}>{t.yourPreferences}</span>
      <div className={styles.chips}>
        {items.map((item, index) => (
          <span key={`${item}-${index}`} className={styles.chip}>
            {displayPreference(item, lang)}
          </span>
        ))}
      </div>
    </div>
  );
}
