import type { QueryPlan, SearchIntent } from "../types/recommend";
import { useI18n } from "../i18n";
import styles from "./IntentPanel.module.css";

interface Props {
  intent: SearchIntent;
  queryPlan?: QueryPlan | null;
}

// 「システムがクエリをどう解釈したか」を見せるパネル（開発者ビュー）。
export default function IntentPanel({ intent, queryPlan }: Props) {
  const { t } = useI18n();
  const { attribute_filters, keywords, price_max, min_rating } = intent;
  const isEmpty = attribute_filters.length === 0 && keywords.length === 0;

  return (
    <section className={styles.panel}>
      <h2 className={styles.heading}>{t.intentHeading}</h2>

      {isEmpty ? (
        <p className={styles.warning}>{t.intentWarning}</p>
      ) : (
        <div className={styles.rows}>
          <Row label={t.attrFilters}>
            {attribute_filters.length === 0 ? (
              <span className={styles.none}>{t.none}</span>
            ) : (
              <div className={styles.chips}>
                {attribute_filters.map((f, i) => (
                  <span key={`${f.attribute_type}-${f.value}-${i}`} className={styles.attrChip}>
                    <span className={styles.attrType}>{f.attribute_type}</span>
                    <span className={styles.attrValue}>{f.value}</span>
                    <span className={styles.weight}>×{f.weight}</span>
                  </span>
                ))}
              </div>
            )}
          </Row>

          <Row label={t.keywords}>
            {keywords.length === 0 ? (
              <span className={styles.none}>{t.none}</span>
            ) : (
              <div className={styles.chips}>
                {keywords.map((k, i) => (
                  <span key={`${k}-${i}`} className={styles.keywordChip}>
                    {k}
                  </span>
                ))}
              </div>
            )}
          </Row>

          <Row label={t.maxPrice}>
            <span className={styles.scalar}>
              {price_max != null ? `¥${price_max.toLocaleString()}` : t.notSet}
            </span>
          </Row>

          <Row label={t.minRating}>
            <span className={styles.scalar}>
              {min_rating != null ? `★${min_rating}` : t.notSet}
            </span>
          </Row>

          {queryPlan && (
            <>
              <Row label="Actions">
                <div className={styles.chips}>
                  {queryPlan.actions
                    .filter((action) => action.enabled)
                    .map((action) => (
                      <span key={action.name} className={styles.actionChip} title={action.reason}>
                        {action.name}
                      </span>
                    ))}
                </div>
              </Row>

              <Row label="Safety">
                <ul className={styles.notes}>
                  {queryPlan.safety_notes.map((note) => (
                    <li key={note}>{note}</li>
                  ))}
                </ul>
              </Row>
            </>
          )}
        </div>
      )}
    </section>
  );
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className={styles.row}>
      <span className={styles.rowLabel}>{label}</span>
      <div className={styles.rowBody}>{children}</div>
    </div>
  );
}
