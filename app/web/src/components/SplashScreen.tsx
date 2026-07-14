import { useEffect, useState, type CSSProperties } from "react";
import pixelRunner from "../assets/pixel-runner-v2.png";
import styles from "./SplashScreen.module.css";
import type { RecommendationTool } from "../types/tool";

interface Props {
  onDone: () => void;
  onStart?: (tool: RecommendationTool) => void;
  minDurationMs?: number;
}

// サイトに入った瞬間に表示する、8-bit横スクロール風の起動アニメーション。
// minDurationMs経過後にタイトルメニューを出し、ユーザーが開始を選ぶとフェードアウトする。
export default function SplashScreen({ onDone, onStart, minDurationMs = 3200 }: Props) {
  const [showMenu, setShowMenu] = useState(false);
  const [fadingOut, setFadingOut] = useState(false);

  useEffect(() => {
    const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const showTimer = setTimeout(() => setShowMenu(true), reduceMotion ? 700 : minDurationMs);
    return () => clearTimeout(showTimer);
  }, [minDurationMs]);

  useEffect(() => {
    if (!fadingOut) return;
    const doneTimer = setTimeout(onDone, 520);
    return () => clearTimeout(doneTimer);
  }, [fadingOut, onDone]);

  function startTool(tool: RecommendationTool) {
    if (!fadingOut) {
      onStart?.(tool);
      setFadingOut(true);
    }
  }

  const durationStyle = {
    "--splash-duration": `${minDurationMs}ms`,
  } as CSSProperties;

  return (
    <div
      className={`${styles.overlay} ${fadingOut ? styles.fadeOut : ""}`}
      style={durationStyle}
      role="dialog"
      aria-label="Game Concierge title screen"
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

      {!showMenu ? (
        <div className={styles.loading}>
          <div className={styles.loadingRow}>
            <span>NOW LOADING</span>
            <span className={styles.dots}>...</span>
          </div>
          <div className={styles.barTrack}>
            <div className={styles.barFill} />
          </div>
        </div>
      ) : (
        <div className={styles.menu}>
          <p className={styles.menuTitle}>推薦方法を選ぶ</p>
          <button
            type="button"
            className={styles.menuItem}
            onClick={() => startTool("personalized")}
            autoFocus
          >
            <span className={styles.cursor} aria-hidden="true">▶</span>
            <span className={styles.menuCopy}>
              <strong>個人化推薦</strong>
              <small>評価・閲覧履歴から、すぐにおすすめ</small>
            </span>
          </button>
          <button
            type="button"
            className={styles.menuItem}
            onClick={() => startTool("dialogue")}
          >
            <span className={styles.cursor} aria-hidden="true">▶</span>
            <span className={styles.menuCopy}>
              <strong>対話型推薦</strong>
              <small>会話で希望を伝えて、おすすめを探す</small>
            </span>
          </button>
          <p className={styles.menuHint}>使いたいツールを選択してください</p>
        </div>
      )}
    </div>
  );
}
