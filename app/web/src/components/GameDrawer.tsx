import { useEffect, useRef } from "react";
import { createPortal } from "react-dom";
import type { GameCardData } from "../types/card";
import { useI18n } from "../i18n";
import { StatsLine, formatPrice } from "./GameCard";
import ReviewList from "./ReviewList";
import styles from "./GameDrawer.module.css";

interface Props {
  data: GameCardData;
  onClose: () => void;
}

// A panel over the page (from the right, or from the bottom on a phone) with the game's reviews and
// description, so opening them never changes the size of a card in the grid.
export default function GameDrawer({ data, onClose }: Props) {
  const { t } = useI18n();
  const panel = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    panel.current?.focus();
    const overflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = overflow;
      previous?.focus();
    };
  }, [onClose]);

  return createPortal(
    <div className={styles.backdrop} onClick={onClose}>
      <div
        ref={panel}
        className={styles.panel}
        role="dialog"
        aria-modal="true"
        aria-label={data.name}
        tabIndex={-1}
        onClick={(e) => e.stopPropagation()}
      >
        <div className={styles.banner}>
          <img src={data.image_url} alt="" />
          <button type="button" className={styles.close} onClick={onClose} aria-label={t.close}>×</button>
        </div>
        <div className={styles.body}>
          <div className={styles.head}>
            <h2 className={styles.title}>{data.name}</h2>
            <span className={styles.price}>{formatPrice(data.price, data.currency, t.free, t.priceUnknown)}</span>
          </div>
          {data.meta.length > 0 && <p className={styles.meta}>{data.meta.join(" · ")}</p>}
          {data.stats && <StatsLine stats={data.stats} />}
          {data.tags.length > 0 && (
            <div className={styles.tags}>
              {data.tags.map((tag) => <span key={tag}>{tag}</span>)}
            </div>
          )}
          <a className={styles.store} href={data.store_url} target="_blank" rel="noreferrer">{t.openStore} ↗</a>

          {data.description && (
            <section className={styles.section}>
              <h3>{t.description}</h3>
              <p className={styles.description}>{data.description}</p>
            </section>
          )}

          <section className={styles.section}>
            <h3>{t.reviews}</h3>
            {data.readableReviews > 0 ? (
              <ReviewList appid={data.appid} reviewCount={data.readableReviews} autoLoad />
            ) : (
              <p className={styles.empty}>{t.noReviews}</p>
            )}
          </section>
        </div>
      </div>
    </div>,
    document.body,
  );
}
