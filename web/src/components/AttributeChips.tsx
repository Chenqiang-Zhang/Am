import type { MatchedAttr } from "../types/recommend";
import { useI18n } from "../i18n";
import styles from "./AttributeChips.module.css";

interface Props {
  attributes: MatchedAttr[];
}

// 推薦根拠として最も精密な「一致した構造化属性」。
// 空の場合の表示が重要：属性カバレッジ <1% の問題を可視化する。
export default function AttributeChips({ attributes }: Props) {
  const { t } = useI18n();
  if (attributes.length === 0) {
    return <span className={styles.empty}>{t.noAttributeMatch}</span>;
  }

  return (
    <div className={styles.chips}>
      {attributes.map((a, i) => (
        <span key={`${a.attr_type}-${a.value}-${i}`} className={styles.chip}>
          <span className={styles.type}>{a.attr_type}</span>
          <span className={styles.value}>{a.value}</span>
        </span>
      ))}
    </div>
  );
}
