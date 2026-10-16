import type { Reason } from "../types/recommend";
import { useI18n } from "../i18n";
import styles from "./ReasonChips.module.css";

interface Props {
  reasons: Reason[];
}

export default function ReasonChips({ reasons }: Props) {
  const { t } = useI18n();
  if (reasons.length === 0) return null;

  return (
    <div className={styles.chips}>
      {reasons.map((r, i) => (
        <span key={`${r.kind}-${r.label}-${i}`} className={styles.chip}>
          <span className={styles.type}>{t.reasonKinds[r.kind]}</span>
          <span className={styles.value}>{r.label}</span>
        </span>
      ))}
    </div>
  );
}
