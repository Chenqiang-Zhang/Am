import styles from "./HeroIllustration.module.css";

// 会話が始まる前の最初の画面に表示する装飾イラスト。実商品画像とは無関係の
// 純粋なUIチェコとして、コントローラー＋星のSVGをインラインで自作している
// （外部アセット依存なし、ダークテーマのアクセントカラーと同じグラデーションを使用）。
export default function HeroIllustration() {
  return (
    <div className={styles.wrap} aria-hidden="true">
      <svg
        className={styles.svg}
        viewBox="0 0 200 140"
        fill="none"
        xmlns="http://www.w3.org/2000/svg"
      >
        <defs>
          <linearGradient id="heroGrad" x1="0" y1="0" x2="1" y2="1">
            <stop offset="0%" stopColor="var(--color-primary)" />
            <stop offset="100%" stopColor="var(--color-accent)" />
          </linearGradient>
        </defs>

        <circle className={styles.glow} cx="100" cy="70" r="55" fill="url(#heroGrad)" opacity="0.18" />

        {/* ゲームパッド本体 */}
        <path
          d="M55 78c-4-18 8-34 27-34h36c19 0 31 16 27 34l-3 14c-2 10-13 16-22 11l-6-3a24 24 0 0 0-22 0l-6 3c-9 5-20-1-22-11z"
          fill="url(#heroGrad)"
        />
        {/* 十字キー */}
        <rect x="70" y="58" width="8" height="22" rx="2" fill="var(--color-bg)" />
        <rect x="62" y="66" width="24" height="8" rx="2" fill="var(--color-bg)" />
        {/* ボタン */}
        <circle cx="130" cy="60" r="4.5" fill="var(--color-bg)" />
        <circle cx="142" cy="72" r="4.5" fill="var(--color-bg)" />
        <circle cx="118" cy="72" r="4.5" fill="var(--color-bg)" />
        <circle cx="130" cy="84" r="4.5" fill="var(--color-bg)" />

        {/* 装飾の星 */}
        <g className={styles.sparkleA}>
          <path d="M35 30l3 8 8 3-8 3-3 8-3-8-8-3 8-3z" fill="var(--color-gold)" />
        </g>
        <g className={styles.sparkleB}>
          <path d="M168 40l2.4 6.4 6.4 2.4-6.4 2.4-2.4 6.4-2.4-6.4-6.4-2.4 6.4-2.4z" fill="var(--color-accent)" />
        </g>
        <g className={styles.sparkleC}>
          <circle cx="150" cy="100" r="3" fill="var(--color-primary)" />
        </g>
      </svg>
    </div>
  );
}
