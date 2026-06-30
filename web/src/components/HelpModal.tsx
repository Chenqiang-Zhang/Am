import { useEffect } from "react";
import { useI18n } from "../i18n";
import styles from "./HelpModal.module.css";

interface Props {
  onClose: () => void;
}

const STEPS_JA = [
  {
    icon: "💬",
    title: "欲しいものを話しかける",
    body: "「保湿クリームが欲しい」「プレゼントに使えるコスメ」など、自然な言葉で入力してください。",
  },
  {
    icon: "🤖",
    title: "AIが好みを深掘りする",
    body: "価格帯・用途・成分などをさらに質問します。ボタンから選ぶか、テキストで答えてください。",
  },
  {
    icon: "✨",
    title: "おすすめ商品が表示される",
    body: "条件が揃うと推薦リストが表示されます。「詳細を見る」でレビュー根拠、「レビューを見る」で実際の口コミを確認できます。",
  },
  {
    icon: "🔄",
    title: "リセットしてやり直す",
    body: "違う商品を探したいときは入力欄横の「リセット」ボタンで会話をクリアできます。",
  },
];

const STEPS_EN = [
  {
    icon: "💬",
    title: "Describe what you want",
    body: 'Type in natural language — e.g. "a good moisturizer" or "a gift-worthy skincare set".',
  },
  {
    icon: "🤖",
    title: "AI asks follow-up questions",
    body: "It may ask about price range, purpose, or ingredients. Pick from the buttons or type your own answer.",
  },
  {
    icon: "✨",
    title: "Recommendations appear",
    body: 'Once it has enough info, a product list is shown. Use "Show more" for review evidence and "Show reviews" for actual customer reviews.',
  },
  {
    icon: "🔄",
    title: "Reset to start over",
    body: 'Hit the "Reset" button next to the input to clear the conversation and search for something else.',
  },
];

const TIPS_JA = [
  "「おまかせ」ボタンで条件なしにランダム推薦もできます",
  "価格は Amazon データに含まれる場合のみ表示されます（約 16% の商品）",
  "レビューはデータセットに取り込んだ件数のみ表示されます",
];

const TIPS_EN = [
  'Use the "Surprise me" button to get recommendations without any constraints',
  "Prices are shown only when available in the dataset (~16% of products)",
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
