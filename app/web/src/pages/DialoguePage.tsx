import { useMemo, useState } from "react";
import mock from "../mocks/dialogues.json";
import type { Answer, DialogueTurn, RecordedDialogue } from "../types/dialogue";
import { useI18n } from "../i18n";
import ChatBubble from "../components/ChatBubble";
import GameCard from "../components/GameCard";
import { cardFromCandidate } from "../components/cardData";
import DialogueStatePanel from "../components/DialogueStatePanel";
import type { PageControls } from "../components/PageHeader";
import QuestionWhy from "../components/QuestionWhy";
import styles from "./DialoguePage.module.css";

const DIALOGUES = (mock as { dialogues: RecordedDialogue[] }).dialogues;
const ANSWERS: Answer[] = ["yes", "no", "no_preference"];

// Replays recorded dialogues turn by turn. When the dialogue API exists, `turns` will grow from its responses
// and the answer buttons will send the user's choice instead of showing the recorded one.
export default function DialoguePage(controls: PageControls) {
  const { devMode } = controls;
  const { t } = useI18n();
  const d = t.dialogue;
  const [index, setIndex] = useState(0);
  const [shown, setShown] = useState(1);

  const dialogue = DIALOGUES[index];
  const turns = dialogue?.turns ?? [];
  const visible = turns.slice(0, shown);
  const current = visible[visible.length - 1];

  const options = useMemo(
    () =>
      DIALOGUES.map((item, i) => ({
        i,
        label: `${i + 1}. ${d.relationName[item.relation ?? "none"]}${item.anchor ? `（${item.anchor.name}）` : ""}`
          + (devMode ? ` → ${item.target.name}` : ""),
      })),
    [d, devMode],
  );

  function choose(i: number) {
    setIndex(i);
    setShown(1);
  }

  if (!dialogue || !current) {
    return (
      <div className={styles.page}>
      </div>
    );
  }

  return (
    <div className={styles.page}>

      <p className={styles.note}>{d.replayNote}</p>

      <div className={styles.toolbar}>
        <label className={styles.pick}>
          <span>{d.pick}</span>
          <select value={index} onChange={(e) => choose(Number(e.target.value))}>
            {options.map((option) => (
              <option key={option.i} value={option.i}>{option.label}</option>
            ))}
          </select>
        </label>
        <div className={styles.stepper}>
          <button type="button" onClick={() => setShown(shown - 1)} disabled={shown <= 1}>{d.prev}</button>
          <span className={styles.turnOf}>{d.turnOf(shown, turns.length)}</span>
          <button type="button" className={styles.next} onClick={() => setShown(shown + 1)}
                  disabled={shown >= turns.length}>
            {d.next}
          </button>
        </div>
      </div>

      <div className={styles.layout}>
        <section className={styles.conversation}>
          <ChatBubble role="assistant" content={t.greeting} />
          {visible.map((turn, i) => (
            <Turn key={`${dialogue.id}-${turn.turn}`} turn={turn} latest={i === visible.length - 1}
                  last={turn.turn === turns.length} devMode={devMode} />
          ))}
        </section>
        <div className={styles.side}>
          <DialogueStatePanel state={current.state} />
        </div>
      </div>
    </div>
  );
}

interface TurnProps {
  turn: DialogueTurn;
  latest: boolean;
  last: boolean;
  devMode: boolean;
}

function Turn({ turn, latest, last, devMode }: TurnProps) {
  const { t } = useI18n();
  const d = t.dialogue;
  const [expanded, setExpanded] = useState(false);
  const open = latest || expanded;

  const said = turn.user.answer
    ? turn.user.answer.answer && d.answers[turn.user.answer.answer]
    : turn.turn === 1
      ? d.opening(turn.user.include.map((a) => a.label), turn.user.anchors[0]?.name ?? null, turn.user.relation)
      : null;

  return (
    <div className={styles.turn}>
      {said && <ChatBubble role="user" content={said} />}

      <ChatBubble role="assistant" content={d.listIntro(turn.candidates.length)} />
      {open ? (
        <div className={styles.list}>
          {turn.candidates.map((game, i) => (
            <GameCard key={game.appid} data={cardFromCandidate(game, i + 1, t, devMode)} rank={i + 1}
                      devMode={devMode} actions />
          ))}
        </div>
      ) : (
        <button type="button" className={styles.collapsed} onClick={() => setExpanded(true)}>
          <span className={styles.passed}>{d.passed}</span>
          {turn.candidates.map((game) => game.name).join(" · ")}
        </button>
      )}
      {!latest && expanded && (
        <button type="button" className={styles.collapse} onClick={() => setExpanded(false)}>{d.showLess}</button>
      )}

      {turn.hit_rank != null && <ChatBubble role="user" content={d.listHit(turn.hit_rank)} />}

      {turn.question && <ChatBubble role="assistant" content={d.question(turn.question)} />}
      {turn.question && turn.why && (
        <QuestionWhy key={`${turn.turn}-${latest}`} question={turn.question} why={turn.why} initiallyOpen={latest} />
      )}
      {/* Only on the latest turn: on earlier ones the next turn's user bubble already shows the answer. */}
      {turn.question && latest && (
        <div className={styles.answers} aria-label={d.simulatedAnswer}>
          <span className={styles.answerNote}>{d.simulatedAnswer}</span>
          {ANSWERS.map((answer) => (
            <span key={answer} className={answer === turn.answer ? styles.answerPicked : styles.answer}>
              {d.answers[answer]}
            </span>
          ))}
        </div>
      )}
      {!turn.question && turn.hit_rank == null && !last && <ChatBubble role="assistant" content={d.stopped} />}
    </div>
  );
}
