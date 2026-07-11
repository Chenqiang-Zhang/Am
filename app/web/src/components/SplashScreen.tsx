import { useEffect, useState, type CSSProperties } from "react";
import pixelRunner from "../assets/pixel-runner-v2.png";
import styles from "./SplashScreen.module.css";

interface Props {
  onDone: () => void;
  minDurationMs?: number;
}

// サイトに入った瞬間に表示する、8-bit横スクロール風の起動アニメーション。
// minDurationMs経過後にフェードアウトを開始し、トランジション終了後にonDone()でApp側から
// アンマウントしてもらう（アンマウントを急に行うとチラつくため、フェード分の余韻を持たせる）。
export default function SplashScreen({ onDone, minDurationMs = 3200 }: Props) {
  const [fadingOut, setFadingOut] = useState(false);

  useEffect(() => {
    const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const showTimer = setTimeout(() => setFadingOut(true), reduceMotion ? 700 : minDurationMs);
    return () => clearTimeout(showTimer);
  }, [minDurationMs]);

  useEffect(() => {
    if (!fadingOut) return;
    const doneTimer = setTimeout(onDone, 520);
    return () => clearTimeout(doneTimer);
  }, [fadingOut, onDone]);

  const durationStyle = {
    "--splash-duration": `${minDurationMs}ms`,
  } as CSSProperties;

  return (
    <div
      className={`${styles.overlay} ${fadingOut ? styles.fadeOut : ""}`}
      style={durationStyle}
      aria-hidden="true"
    >
      <div className={styles.pixelGrid} />
      <div className={styles.stars}>
        <i /><i /><i /><i /><i /><i />
      </div>

      <div className={styles.brand}>
        <span className={styles.eyebrow}>GAME SELECT SYSTEM</span>
        <strong>GAME CONCIERGE</strong>
      </div>

      <div className={styles.scene}>
        <div className={styles.speedLines}><i /><i /><i /><i /></div>
        <div className={styles.runnerTravel}>
          <img className={styles.runner} src={pixelRunner} alt="" />
        </div>
        <div className={styles.ground} />
      </div>

      <div className={styles.loading}>
        <div className={styles.loadingRow}>
          <span>NOW LOADING</span>
          <span className={styles.dots}>...</span>
        </div>
        <div className={styles.barTrack}>
          <div className={styles.barFill} />
        </div>
      </div>
    </div>
  );
}
