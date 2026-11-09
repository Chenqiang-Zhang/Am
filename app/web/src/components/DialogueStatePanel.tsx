import type { DialogueState } from "../types/dialogue";
import { useI18n } from "../i18n";
import styles from "./DialogueStatePanel.module.css";

interface Props {
  state: DialogueState;
}

// The conditions the dialogue holds across turns: the part a plain chat loses when it re-reads the history.
export default function DialogueStatePanel({ state }: Props) {
  const { t } = useI18n();
  const d = t.dialogue;
  const anchor = state.anchors.map((game) => game.name).join(", ");

  return (
    <aside className={styles.panel} aria-label={d.state}>
      <h2 className={styles.heading}>{d.state}</h2>
      <dl className={styles.rows}>
        <dt>{d.want}</dt>
        <dd>
          {state.include.length === 0 && <span className={styles.none}>{d.none}</span>}
          {state.include.map((a) => (
            <span key={a.key} className={styles.want}>{a.label}</span>
          ))}
        </dd>
        <dt>{d.avoid}</dt>
        <dd>
          {state.exclude.length === 0 && <span className={styles.none}>{d.none}</span>}
          {state.exclude.map((a) => (
            <span key={a.key} className={styles.avoid}>{a.label}</span>
          ))}
        </dd>
        <dt>{d.budget}</dt>
        <dd>{d.priceRange(state.min_price, state.max_price)}</dd>
        <dt>{d.anchor}</dt>
        <dd>
          {anchor ? (
            <>
              <span className={styles.anchor}>{anchor}</span>
              <span className={styles.relation}>{d.relationName[state.relation ?? "none"]}</span>
            </>
          ) : (
            <span className={styles.none}>{d.none}</span>
          )}
        </dd>
      </dl>
    </aside>
  );
}
