import { useState } from "react";
import type { ReviewItem } from "../types/recommend";
import { useI18n } from "../i18n";
import { fetchReviews } from "../api/client";
import styles from "./ReviewList.module.css";

interface Props {
  appid: string;
  reviewCount: number;
}

type Status = "idle" | "loading" | "done" | "error";

export default function ReviewList({ appid, reviewCount }: Props) {
  const { t } = useI18n();
  const [status, setStatus] = useState<Status>("idle");
  const [reviews, setReviews] = useState<ReviewItem[]>([]);
  const [open, setOpen] = useState(true);

  async function load() {
    if (status !== "idle") return;
    setStatus("loading");
    try {
      const data = await fetchReviews(appid, reviewCount);
      setReviews(data.reviews);
      setStatus("done");
    } catch {
      setStatus("error");
    }
  }

  if (status === "idle" || (status === "done" && !open)) {
    return (
      <button
        type="button"
        className={styles.trigger}
        onClick={status === "idle" ? load : () => setOpen(true)}
        aria-expanded={false}
      >
        <span className={styles.triggerIcon} aria-hidden="true">★</span>
        <span>{t.showReviews}</span>
        <span className={styles.chevron} aria-hidden="true">›</span>
      </button>
    );
  }

  if (status === "loading") return <p className={styles.loading}>{t.loading}</p>;
  if (status === "error") return <p className={styles.error}>{t.reviewsError}</p>;

  return (
    <div className={styles.block}>
      <div className={styles.header}>
        <span className={styles.heading}>
          <span className={styles.panelIcon} aria-hidden="true">★</span>
          {t.reviews}
        </span>
        <span className={styles.count}>{t.reviewsShown(reviews.length, reviewCount)}</span>
        <button type="button" className={styles.closeBtn} onClick={() => setOpen(false)} aria-expanded={true}>
          {t.close} <span aria-hidden="true">×</span>
        </button>
      </div>
      {reviews.length === 0 ? (
        <p className={styles.empty}>{t.noReviews}</p>
      ) : (
        <>
          <ul className={styles.list} tabIndex={0} aria-label={t.reviews}>
            {reviews.map((r, i) => (
              <ReviewRow key={i} review={r} />
            ))}
          </ul>
          {reviewCount > reviews.length && <p className={styles.partial}>{t.reviewsPartial}</p>}
        </>
      )}
    </div>
  );
}

function ReviewRow({ review }: { review: ReviewItem }) {
  const { t } = useI18n();
  return (
    <li className={styles.item}>
      <div className={styles.itemHead}>
        {review.recommended != null && (
          <span className={styles.badge}>{review.recommended ? t.recommended : t.notRecommended}</span>
        )}
        {review.hours != null && <span className={styles.helpful}>{t.played(review.hours)}</span>}
        {(review.votes_up ?? 0) > 0 && <span className={styles.helpful}>{t.votesUp(review.votes_up!)}</span>}
      </div>
      <p className={styles.reviewText}>{review.text}</p>
    </li>
  );
}
