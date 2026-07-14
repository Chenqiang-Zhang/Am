import { useEffect } from "react";
import { useI18n } from "../i18n";
import styles from "./HelpModal.module.css";

interface Props {
  onClose: () => void;
}

const STEPS_JA = [
  {
    icon: "🎮",
    title: "2つの推薦ツールから選ぶ",
    body: "起動画面または画面上部の切り替えボタンから、「個人化推薦」と「対話型推薦」を選べます。",
  },
  {
    icon: "✨",
    title: "個人化推薦：履歴からすぐに探す",
    body: "選択中のユーザーの評価・閲覧履歴をもとに、ゲームを自動で提案します。会話を始める必要はありません。",
  },
  {
    icon: "💬",
    title: "対話型推薦：希望を話して探す",
    body: "欲しいゲームを自然な言葉で入力してください。会話の途中から現在の候補が表示され、AIの質問に答えるたびに内容と順番が更新されます。",
  },
  {
    icon: "🔄",
    title: "いつでも切り替える",
    body: "画面上部のボタンで2つのツールを行き来できます。対話内容は切り替えても保持され、「最初から」でリセットできます。",
  },
];

const STEPS_EN = [
  {
    icon: "🎮",
    title: "Choose one of two tools",
    body: "Pick Personalized or Conversational recommendations on the title screen or with the switcher at the top.",
  },
  {
    icon: "✨",
    title: "Personalized: get instant picks",
    body: "Games are suggested automatically from the selected user's ratings and viewing history. No conversation is required.",
  },
  {
    icon: "💬",
    title: "Conversational: describe what you want",
    body: "Type your request naturally. Current candidates appear during the chat and update whenever you answer a follow-up question.",
  },
  {
    icon: "🔄",
    title: "Switch at any time",
    body: 'Move between tools with the buttons at the top. Your chat is kept until you choose "Start over".',
  },
];

const TIPS_JA = [
  "ユーザー選択と開発者向け機能は、右上の歯車から変更できます",
  "商品カードの「レビューを見る」で、データセットに含まれる口コミを確認できます",
  "レビューはデータセットに取り込んだ件数のみ表示されます",
];

const TIPS_EN = [
  "Use the gear in the top-right to change the demo user or open developer options",
  "Use Show reviews on a product card to inspect reviews in the dataset",
  "Reviews shown are limited to what was imported into our dataset",
];

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
