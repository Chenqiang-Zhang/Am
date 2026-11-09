import { useState } from "react";
import type { Attribute, QuestionWhy as Why } from "../types/dialogue";
import { useI18n } from "../i18n";
import styles from "./QuestionWhy.module.css";

interface Props {
  question: Attribute;
  why: Why;
  initiallyOpen: boolean;
}

// How the candidate pool splits on the question asked, and the questions that came next.
export default function QuestionWhy({ question, why, initiallyOpen }: Props) {
  const { t } = useI18n();
  const w = t.dialogue.why;
  const [open, setOpen] = useState(initiallyOpen);
  const yesShare = why.pool > 0 ? (why.yes / why.pool) * 100 : 0;

  return (
    <div className={styles.box}>
      <button type="button" className={styles.toggle} onClick={() => setOpen(!open)} aria-expanded={open}>
        <span className={styles.icon} aria-hidden="true">?</span>
        {w.title}
        <span className={styles.chevron} aria-hidden="true">{open ? "▴" : "▾"}</span>
      </button>
      {open && (
        <div className={styles.body}>
          <p className={styles.summary}>{w.summary(why.pool, w.yes(question), why.yes, why.no)}</p>

          <div className={styles.split} role="img" aria-label={w.summary(why.pool, w.yes(question), why.yes, why.no)}>
            <div className={styles.yes} style={{ width: `${yesShare}%` }}>
              <span>{w.yes(question)}</span>
              <strong>{why.yes}</strong>
            </div>
            <div className={styles.no} style={{ width: `${100 - yesShare}%` }}>
              <span>{w.no(question)}</span>
              <strong>{why.no}</strong>
            </div>
          </div>

          <p className={styles.reason}>{w.reason}</p>

          <div className={styles.meters}>
            <div className={styles.legend}>{w.entropy}</div>
            <div className={styles.meterRow}>
              <span className={styles.meterLabel}>{w.yes(question)}</span>
              <span className={styles.track}>
                <span className={styles.fillChosen} style={{ width: `${why.entropy * 100}%` }} />
              </span>
              <span className={styles.value}>{why.entropy.toFixed(3)}</span>
            </div>
            {why.alternatives.length > 0 && <div className={styles.legend}>{w.alternatives}</div>}
            {why.alternatives.map((alt) => (
              <div key={alt.attribute.key} className={styles.meterRow}>
                <span className={styles.meterLabelMuted}>{w.yes(alt.attribute)}</span>
                <span className={styles.track}>
                  <span className={styles.fill} style={{ width: `${alt.entropy * 100}%` }} />
                </span>
                <span className={styles.value}>{alt.entropy.toFixed(3)}</span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
