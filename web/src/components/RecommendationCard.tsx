import { useState } from "react";
import type { Recommendation } from "../types/recommend";
import { useI18n } from "../i18n";
import type { Lang } from "../i18n";
import { friendlyTags, evidenceQuote } from "../lib/explain";
import { useUsdToJpy } from "../lib/exchangeRate";
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
import TermChips from "./TermChips";
import EvidenceList from "./EvidenceList";
import ScoreBreakdown from "./ScoreBreakdown";
import styles from "./RecommendationCard.module.css";

interface Props {
  rec: Recommendation;
  rank: number;
  devMode: boolean;
}

// 1 商品の説明カード。既定（ユーザーモード）はやさしい表示、devMode で機械的な根拠データ。
export default function RecommendationCard({ rec, rank, devMode }: Props) {
  const { lang } = useI18n();
  const jpyRate = useUsdToJpy();
  return (
    <article className={styles.card} style={{ animationDelay: `${(rank - 1) * 70}ms` }}>
      <ProductImage src={rec.image_url} alt={rec.title} />
      <div className={styles.body}>
        <div className={styles.head}>
          {devMode && <span className={styles.rank}>#{rank}</span>}
          <h3 className={styles.title} title={rec.title}>
            {rec.title}
          </h3>
          {devMode && (
            <span className={styles.score} title="score">
              {rec.score.toFixed(2)}
            </span>
          )}
        </div>

        <div className={styles.meta}>
          <RatingStars rating={rec.average_rating} count={rec.rating_number} />
          <span className={rec.price != null ? styles.price : styles.priceUnknown}>
            {rec.price != null
              ? formatPrice(rec.price, lang, jpyRate)
              : (lang === "ja" ? "価格情報なし" : "Price unavailable")}
          </span>
          {devMode && <span className={styles.id}>{rec.product_id}</span>}
        </div>

        {devMode ? <DevExplanation rec={rec} /> : <UserExplanation rec={rec} />}
        <ReviewList productId={rec.product_id} ratingNumber={rec.rating_number} />
        <a
          className={styles.amazonLink}
          href={`https://www.amazon.com/dp/${rec.product_id}`}
          target="_blank"
          rel="noopener noreferrer"
        >
          {lang === "ja" ? "Amazon.com で見る →" : "View on Amazon.com →"}
        </a>
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
          <rect x="2" y="6" width="20" height="14" rx="2" stroke="currentColor" strokeWidth="1.5"/>
          <circle cx="12" cy="13" r="3.5" stroke="currentColor" strokeWidth="1.5"/>
          <path d="M8 6l1.5-2h5L16 6" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/>
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
  const [expanded, setExpanded] = useState(false);
  const tags = friendlyTags(rec, lang);
  const fullQuote = rec.matched_feature_evidence[0] ?? null;

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
      {fullQuote && (
        <div className={styles.quoteBlock}>
          {expanded && (
            <p className={styles.userQuote}>{'"'}{fullQuote}{'"'}</p>
          )}
          <button
            type="button"
            className={styles.quoteToggle}
            onClick={() => setExpanded((v) => !v)}
          >
            {expanded
              ? (lang === "ja" ? "閉じる" : "Show less")
              : (lang === "ja" ? "詳細を見る" : "Show more")}
          </button>
        </div>
      )}
    </div>
  );
}

// ===== 開発者モード：機械的な根拠データ（ガラス張り） =====
function DevExplanation({ rec }: { rec: Recommendation }) {
  const { t } = useI18n();
  return (
    <div className={styles.dev}>
      <Section label={t.matchedAttributes}>
        <AttributeChips attributes={rec.matched_attributes} />
      </Section>
      <Section label={t.matchedTerms}>
        <TermChips terms={rec.matched_terms} />
      </Section>
      {rec.matched_feature_evidence.length > 0 && (
        <Section label={t.evidence}>
          <EvidenceList evidence={rec.matched_feature_evidence} />
        </Section>
      )}
      <Section label={t.scoreBreakdown}>
        <ScoreBreakdown breakdown={rec.score_breakdown} />
      </Section>
      {rec.explanation && <p className={styles.explanation}>{rec.explanation}</p>}
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
