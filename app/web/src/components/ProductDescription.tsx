import { useState } from "react";
import { fetchDescription } from "../api/client";
import { useI18n } from "../i18n";
import styles from "./ProductDescription.module.css";

interface Props {
  productId: string;
}

type Status = "idle" | "loading" | "done" | "error";

// レビューと同じ「押すまで取得しない」設計。descriptionはText2Cypherの検索結果には
// 含めていない（app/api/recommender.py の get_description() 参照）ため、商品IDを
// 指定して個別に取得する。
export default function ProductDescription({ productId }: Props) {
  const { lang } = useI18n();
  const [status, setStatus] = useState<Status>("idle");
  const [description, setDescription] = useState<string | null>(null);
  const [open, setOpen] = useState(true);

  async function load() {
    if (status !== "idle") return;
    setStatus("loading");
    try {
      const data = await fetchDescription(productId, lang);
      setDescription(data.description);
      setStatus("done");
    } catch {
      setStatus("error");
    }
  }

  if (status === "idle") {
    return (
      <button type="button" className={styles.trigger} onClick={load} aria-expanded={false}>
        <span className={styles.triggerIcon} aria-hidden="true">i</span>
        <span>{lang === "ja" ? "商品説明を見る" : "Show description"}</span>
        <span className={styles.chevron} aria-hidden="true">›</span>
      </button>
    );
  }

  if (status === "loading") {
    return <p className={styles.loading}>{lang === "ja" ? "読み込み中…" : "Loading…"}</p>;
  }

  if (status === "error") {
    return (
      <p className={styles.error}>
        {lang === "ja" ? "商品説明を取得できませんでした" : "Could not load description"}
      </p>
    );
  }

  if (!open) {
    return (
      <button type="button" className={styles.trigger} onClick={() => setOpen(true)} aria-expanded={false}>
        <span className={styles.triggerIcon} aria-hidden="true">i</span>
        <span>{lang === "ja" ? "商品説明を見る" : "Show description"}</span>
        <span className={styles.chevron} aria-hidden="true">›</span>
      </button>
    );
  }

  return (
    <div className={styles.block}>
      <div className={styles.header}>
        <span className={styles.heading}>
          <span className={styles.panelIcon} aria-hidden="true">i</span>
          {lang === "ja" ? "商品説明" : "Description"}
        </span>
        <button type="button" className={styles.closeBtn} onClick={() => setOpen(false)} aria-expanded={true}>
          {lang === "ja" ? "閉じる" : "Close"} <span aria-hidden="true">×</span>
        </button>
      </div>
      <div
        className={styles.content}
        tabIndex={0}
        aria-label={lang === "ja" ? "商品説明本文" : "Product description"}
      >
        {description ? (
          <p className={styles.text}>{description}</p>
        ) : (
          <p className={styles.empty}>{lang === "ja" ? "商品説明がありません" : "No description"}</p>
        )}
      </div>
    </div>
  );
}
