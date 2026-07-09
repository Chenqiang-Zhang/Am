import { useState } from "react";
import type { ReviewItem } from "../types/recommend";
import { useI18n } from "../i18n";
import { fetchReviews, logView } from "../api/client";
import RatingStars from "./RatingStars";
import styles from "./ReviewList.module.css";

interface Props {
  productId: string;
  ratingNumber?: number | null;
  userId: string;
  searchId: string | null;
}

type Status = "idle" | "loading" | "done" | "error";

export default function ReviewList({ productId, ratingNumber, userId, searchId }: Props) {
  const { lang, t } = useI18n();
  const [status, setStatus] = useState<Status>("idle");
  const [reviews, setReviews] = useState<ReviewItem[]>([]);
  const [open, setOpen] = useState(true);

  async function load() {
    if (status !== "idle") return;
    setStatus("loading");
    // レビュー詳細を見る = VIEWED（商品への強い関心のシグナル）として記録する
    logView({ user_id: userId, product_id: productId, search_id: searchId });
    try {
      const data = await fetchReviews(productId, 5);
      setReviews(data.reviews);
      setStatus("done");
    } catch {
      setStatus("error");
    }
  }

  if (status === "idle") {
    return (
      <button type="button" className={styles.trigger} onClick={load}>
        {lang === "ja" ? "レビューを見る" : "Show reviews"} ▼
      </button>
    );
  }

  if (status === "loading") {
    return <p className={styles.loading}>{lang === "ja" ? "読み込み中…" : "Loading…"}</p>;
  }

  if (status === "error") {
    return <p className={styles.error}>{lang === "ja" ? "レビューを取得できませんでした" : "Could not load reviews"}</p>;
  }

  if (!open) {
    return (
      <button type="button" className={styles.trigger} onClick={() => setOpen(true)}>
        {lang === "ja" ? "レビューを見る" : "Show reviews"} ▼
      </button>
    );
  }

  const hasMore = ratingNumber != null && ratingNumber > reviews.length;

  return (
    <div className={styles.block}>
      <div className={styles.header}>
        <span className={styles.heading}>{lang === "ja" ? "レビュー" : "Reviews"}</span>
        <span className={styles.count}>
          {reviews.length > 0
            ? (lang === "ja" ? `${reviews.length}件表示` : `${reviews.length} shown`)
            : ""}
          {ratingNumber != null && ratingNumber > 0 && (
            <span className={styles.countTotal}>
              {lang === "ja" ? ` / Amazon総数 ${ratingNumber.toLocaleString()}件` : ` / ${ratingNumber.toLocaleString()} on Amazon`}
            </span>
          )}
        </span>
        <button type="button" className={styles.closeBtn} onClick={() => setOpen(false)}>
          {lang === "ja" ? "閉じる" : "Close"} ▲
        </button>
      </div>
      {reviews.length === 0 ? (
        <p className={styles.empty}>
          {ratingNumber && ratingNumber > 0
            ? (lang === "ja"
                ? `Amazonには${ratingNumber.toLocaleString()}件のレビューがありますが、このシステムには取り込まれていません`
                : `${ratingNumber.toLocaleString()} Amazon reviews exist but were not imported into this system`)
            : (lang === "ja" ? "レビューがありません" : "No reviews")}
        </p>
      ) : (
        <>
          <ul className={styles.list}>
            {reviews.map((r, i) => (
              <ReviewRow key={i} review={r} lang={lang} />
            ))}
          </ul>
          {hasMore && (
            <p className={styles.partial}>
              {lang === "ja"
                ? `※ データセットに含まれるレビューのみ表示（Amazon上の全${ratingNumber!.toLocaleString()}件のうち一部）`
                : `※ Showing only reviews available in our dataset (out of ${ratingNumber!.toLocaleString()} on Amazon)`}
            </p>
          )}
        </>
      )}
    </div>
  );
}

function ReviewRow({ review, lang }: { review: ReviewItem; lang: string }) {
  return (
    <li className={styles.item}>
      <div className={styles.itemHead}>
        <RatingStars rating={review.rating} count={null} />
        {review.verified_purchase && (
          <span className={styles.verified}>
            {lang === "ja" ? "購入済み" : "Verified"}
          </span>
        )}
        {(review.helpful_vote ?? 0) > 0 && (
          <span className={styles.helpful}>
            {lang === "ja" ? `参考になった ${review.helpful_vote}件` : `${review.helpful_vote} helpful`}
          </span>
        )}
      </div>
      {review.title && <p className={styles.reviewTitle}>{review.title}</p>}
      <p className={styles.reviewText}>{review.text}</p>
    </li>
  );
}
