import { useI18n } from "../i18n";
import styles from "./TermChips.module.css";

interface Props {
  terms: string[];
}

// 特徴テキスト・フィールドで一致した語。
export default function TermChips({ terms }: Props) {
  const { t } = useI18n();
  if (terms.length === 0) {
    return <span className={styles.none}>{t.noTermMatch}</span>;
  }
  return (
    <div className={styles.chips}>
      {terms.map((term, i) => (
        <span key={`${term}-${i}`} className={styles.chip}>
          {term}
        </span>
      ))}
    </div>
  );
}
