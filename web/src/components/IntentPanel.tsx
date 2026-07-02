import type { SearchIntent } from "../types/recommend";
import { useI18n } from "../i18n";
import styles from "./IntentPanel.module.css";

interface Props {
  intent: SearchIntent;
}

// 「システムがクエリをどう解釈したか」を見せるパネル（開発者ビュー）。
// Text2Cypher: LLM の一文説明を主役にし、実行された生Cypherは折りたたみで補足する。
export default function IntentPanel({ intent }: Props) {
  const { t } = useI18n();

  return (
    <section className={styles.panel}>
      <h2 className={styles.heading}>{t.intentHeading}</h2>
      <p className={styles.explanation}>
        {intent.cypher_explanation || t.intentWarning}
      </p>
      {intent.cypher && (
        <details className={styles.cypherDetails}>
          <summary className={styles.cypherSummary}>{t.showCypher}</summary>
          <pre className={styles.cypherCode}>{intent.cypher}</pre>
        </details>
      )}
    </section>
  );
}
