import type { SearchIntent } from "../types/recommend";
import { useI18n } from "../i18n";
import styles from "./IntentPanel.module.css";

interface Props {
  intent: SearchIntent;
}

// 「システムがクエリをどう解釈したか」を見せるパネル（開発者ビュー）。
// 元パス検索: 一文説明を主役にし、実行されたCypherは折りたたみで補足する。
export default function IntentPanel({ intent }: Props) {
  const { t, lang } = useI18n();
  const hardConditions = intent.hard_conditions ?? [];
  const softConditions = intent.soft_conditions ?? [];

  return (
    <section className={styles.panel}>
      <h2 className={styles.heading}>{t.intentHeading}</h2>
      <p className={styles.explanation}>
        {intent.cypher_explanation || t.intentWarning}
      </p>
      {(hardConditions.length > 0 || softConditions.length > 0) && (
        <div className={styles.conditionGroups}>
          <div>
            <h3>{lang === "ja" ? "必須条件" : "Required"}</h3>
            <div className={styles.conditions}>
              {hardConditions.map((condition) => (
                <span key={condition} className={styles.hardCondition}>{condition}</span>
              ))}
            </div>
          </div>
          <div>
            <h3>{lang === "ja" ? "希望条件（順位付け）" : "Preferences (ranking)"}</h3>
            <div className={styles.conditions}>
              {softConditions.map((condition) => (
                <span key={condition} className={styles.softCondition}>{condition}</span>
              ))}
            </div>
          </div>
        </div>
      )}
      {intent.cypher && (
        <details className={styles.cypherDetails}>
          <summary className={styles.cypherSummary}>{t.showCypher}</summary>
          <pre className={styles.cypherCode}>{intent.cypher}</pre>
        </details>
      )}
    </section>
  );
}
