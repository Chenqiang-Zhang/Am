import { useState } from "react";
import { sendFeedback } from "../api/client";
import { useI18n } from "../i18n";
import styles from "./FeedbackButtons.module.css";

interface Props {
  productId: string;
  userId: string | null;
  searchId: string | null;
}

type Status = "idle" | "sending" | "sent" | "error";

// ProductDescription と同じ「状態機械 + インライン日英ターナリ」設計。
// 送信結果はNeo4jのFeedbackノードに保存され、_get_dynamic_few_shot()の
// 正例シグナルとして使われる（app/api/recommender.py の save_feedback() 参照）。
export default function FeedbackButtons({ productId, userId, searchId }: Props) {
  const { lang } = useI18n();
  const [status, setStatus] = useState<Status>("idle");

  async function submit(helpful: boolean) {
    if (status === "sending") return;
    setStatus("sending");
    try {
      await sendFeedback(productId, helpful, userId, searchId, lang);
      setStatus("sent");
    } catch {
      setStatus("error");
    }
  }

  if (status === "sent") {
    return (
      <div className={styles.container}>
        <span className={styles.sentMessage}>
          {lang === "ja" ? "フィードバックありがとうございます" : "Thanks for your feedback!"}
        </span>
      </div>
    );
  }

  return (
    <div className={styles.container}>
      <span className={styles.question}>
        {lang === "ja" ? "この推薦は役に立ちましたか？" : "Was this recommendation helpful?"}
      </span>
      <button
        type="button"
        className={styles.button}
        disabled={status === "sending"}
        onClick={() => submit(true)}
      >
        {lang === "ja" ? "はい" : "Yes"}
      </button>
      <button
        type="button"
        className={styles.button}
        disabled={status === "sending"}
        onClick={() => submit(false)}
      >
        {lang === "ja" ? "いいえ" : "No"}
      </button>
      {status === "error" && (
        <span className={styles.errorMessage}>
          {lang === "ja" ? "送信できませんでした" : "Could not send"}
        </span>
      )}
    </div>
  );
}
