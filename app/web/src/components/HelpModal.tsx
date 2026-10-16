import { useEffect } from "react";
import { useI18n } from "../i18n";
import styles from "./HelpModal.module.css";

interface Props {
  onClose: () => void;
}

const STEPS_JA = [
  {
    icon: "💬",
    title: "遊びたいゲームを話しかける",
    body: "「友達と協力プレイできる、笑えるパズルゲーム」など、自然な言葉で入力してください。",
  },
  {
    icon: "✨",
    title: "おすすめが表示される",
    body: "タグやレビューで語られている観点をもとに、候補を選んで理由つきで表示します。「レビューを見る」で実際のレビューを確認できます。",
  },
  {
    icon: "🎮",
    title: "プレイヤーを選ぶ",
    body: "右上でプレイヤーを選ぶと、そのプレイヤーの遊んだ履歴を踏まえて答えます。",
  },
  {
    icon: "🔄",
    title: "リセットしてやり直す",
    body: "違うゲームを探したいときは、入力欄横の「最初から」ボタンで会話をクリアできます。",
  },
];

const STEPS_EN = [
  {
    icon: "💬",
    title: "Describe the game you want",
    body: 'Type in natural language — e.g. "a funny co-op puzzle game to play with friends".',
  },
  {
    icon: "✨",
    title: "Recommendations appear",
    body: 'Candidates are chosen from tags and the aspects reviewers talk about, and shown with reasons. Use "Show reviews" to read actual reviews.',
  },
  {
    icon: "🎮",
    title: "Pick a player",
    body: "Choose a player at the top right and answers take that player's play history into account.",
  },
  {
    icon: "🔄",
    title: "Start over",
    body: 'Hit the "Start over" button next to the input to clear the conversation.',
  },
];

const TIPS_JA = ["レビューは、取り込み済みのものだけが表示されます"];

const TIPS_EN = ["Only imported reviews are shown"];

export default function HelpModal({ onClose }: Props) {
  const { lang } = useI18n();
  const steps = lang === "ja" ? STEPS_JA : STEPS_EN;
  const tips = lang === "ja" ? TIPS_JA : TIPS_EN;

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div className={styles.overlay} onClick={onClose} role="dialog" aria-modal="true">
      <div className={styles.panel} onClick={(e) => e.stopPropagation()}>
        <div className={styles.panelHeader}>
          <h2 className={styles.panelTitle}>
            {lang === "ja" ? "使い方" : "How to use"}
          </h2>
          <button type="button" className={styles.closeBtn} onClick={onClose} aria-label="close">
            ✕
          </button>
        </div>

        <ol className={styles.steps}>
          {steps.map((s) => (
            <li key={s.title} className={styles.step}>
              <span className={styles.stepIcon}>{s.icon}</span>
              <div>
                <p className={styles.stepTitle}>{s.title}</p>
                <p className={styles.stepBody}>{s.body}</p>
              </div>
            </li>
          ))}
        </ol>

        <div className={styles.tipsBlock}>
          <p className={styles.tipsHeading}>
            {lang === "ja" ? "補足" : "Notes"}
          </p>
          <ul className={styles.tips}>
            {tips.map((tip) => (
              <li key={tip} className={styles.tip}>{tip}</li>
            ))}
          </ul>
        </div>
      </div>
    </div>
  );
}
