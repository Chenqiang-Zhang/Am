import { useState } from "react";
import type { Recommendation } from "../types/recommend";
import { useI18n } from "../i18n";
import type { Lang } from "../i18n";
import { friendlyTags } from "../lib/explain";
import { useUsdToJpy } from "../lib/exchangeRate";
import { logView } from "../api/client";
import ProductDescription from "./ProductDescription";
import ReviewList from "./ReviewList";

function formatPrice(usd: number, lang: Lang, jpyRate: number): string {
  if (lang === "ja") {
    const jpy = Math.round(usd * jpyRate);
    return `¥${jpy.toLocaleString("ja-JP")}`;
  }
  return `$${usd.toFixed(2)}`;
}
import RatingStars from "./RatingStars";
import AttributeChips from "./AttributeChips";
import styles from "./RecommendationCard.module.css";

interface Props {
  rec: Recommendation;
  rank: number;
  devMode: boolean;
  userId: string;
  searchId: string | null;
}

// 1 商品の説明カード。既定（ユーザーモード）はやさしい表示、devMode で機械的な根拠データ。
export default function RecommendationCard({ rec, rank, devMode, userId, searchId }: Props) {
  const { lang } = useI18n();
  const jpyRate = useUsdToJpy();
  return (
    <article className={styles.card} style={{ animationDelay: `${(rank - 1) * 70}ms` }}>
      <ProductImage src={rec.image_url} alt={rec.title} />
      <div className={styles.body}>
        <div className={styles.head}>
          {devMode && <span className={styles.rank}>#{rank}</span>}
          <h3 className={styles.title} title={rec.title}>
            {rec.display_title || rec.title}
          </h3>
          {devMode && (
            <span className={styles.score} title="score">
              {rec.score.toFixed(2)}
            </span>
          )}
        </div>

        <div className={styles.meta}>
          <RatingStars rating={rec.avg_rating} count={rec.rating_count} />
          <span className={rec.price != null ? styles.price : styles.priceUnknown}>
            {rec.price != null
              ? formatPrice(rec.price, lang, jpyRate)
              : (lang === "ja" ? "価格未登録" : "No price in dataset")}
          </span>
          {devMode && <span className={styles.id}>{rec.product_id}</span>}
        </div>

        {devMode ? <DevExplanation rec={rec} /> : <UserExplanation rec={rec} />}
        <ProductDescription productId={rec.product_id} />
        <ReviewList
          productId={rec.product_id}
          ratingNumber={rec.rating_count}
          userId={userId}
          searchId={searchId}
        />
        {/* <a
          className={styles.amazonLink}
          href={`https://www.amazon.com/dp/${rec.product_id}`}
          target="_blank"
          rel="noopener noreferrer"
          onClick={() => {
            logView({ user_id: userId, product_id: rec.product_id, search_id: searchId });
          }}
        >
          {lang === "ja" ? "Amazon.com で見る →" : "View on Amazon.com →"}
        </a> */}
      </div>
    </article>
  );
}

function ProductImage({ src, alt }: { src: string | null; alt: string }) {
  const [failed, setFailed] = useState(false);
  if (!src || failed) {
    return (
      <div className={styles.thumbPlaceholder} aria-hidden="true">
        <svg className={styles.thumbIcon} viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
          <path
            d="M7 9c-2.5 0-4.3 2-3.9 4.4l.6 3.4c.3 1.6 2.1 2.4 3.5 1.5l.7-.4a3.5 3.5 0 0 1 3.3 0l.2.1a3.5 3.5 0 0 0 3.3 0l.2-.1a3.5 3.5 0 0 1 3.3 0l.7.4c1.4.9 3.2.1 3.5-1.5l.6-3.4C23.3 11 21.5 9 19 9z"
            stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round"
          />
          <path d="M8.5 12.5v3M7 14h3" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"/>
          <circle cx="16" cy="12" r="0.9" fill="currentColor"/>
          <circle cx="18.2" cy="14.2" r="0.9" fill="currentColor"/>
        </svg>
      </div>
    );
  }
  return (
    <img
      className={styles.thumb}
      src={src}
      alt={alt}
      loading="lazy"
      onError={() => setFailed(true)}
    />
  );
}

// ===== ユーザーモード：やさしい「おすすめポイント」 =====
function UserExplanation({ rec }: { rec: Recommendation }) {
  const { lang } = useI18n();
  const tags = friendlyTags(rec, lang);

  return (
    <div className={styles.user}>
      {tags.length > 0 && (
        <div className={styles.userTags}>
          {tags.map((t) => (
            <span key={t} className={styles.userTag}>
              {t}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

// ===== 開発者モード：機械的な根拠データ（ガラス張り） =====
// Text2Cypher: 一致した構造化属性 + LLMによる一文説明を主役にする。
function DevExplanation({ rec }: { rec: Recommendation }) {
  const { t } = useI18n();
  return (
    <div className={styles.dev}>
      {rec.explanation && <p className={styles.explanation}>{rec.explanation}</p>}
      <Section label={t.matchedAttributes}>
        <AttributeChips attributes={rec.matched_attrs} />
      </Section>
    </div>
  );
}

function Section({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className={styles.section}>
      <span className={styles.sectionLabel}>{label}</span>
      <div className={styles.sectionBody}>{children}</div>
    </div>
  );
}
