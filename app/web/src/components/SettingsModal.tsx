import { useEffect } from "react";
import { useI18n } from "../i18n";
import TestUserSelect from "./TestUserSelect";
import styles from "./SettingsModal.module.css";

interface Props {
  userId: string;
  onUserChange: (userId: string) => void;
  devMode: boolean;
  onDevModeChange: (enabled: boolean) => void;
  generalMode: boolean;
  onGeneralModeChange: (enabled: boolean) => void;
  clearingHistory: boolean;
  onClearHistory: () => void;
  onClose: () => void;
}

export default function SettingsModal({
  userId,
  onUserChange,
  devMode,
  onDevModeChange,
  generalMode,
  onGeneralModeChange,
  clearingHistory,
  onClearHistory,
  onClose,
}: Props) {
  const { lang, t } = useI18n();

  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if (event.key === "Escape") onClose();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div className={styles.overlay} onClick={onClose} role="dialog" aria-modal="true" aria-labelledby="settings-title">
      <div className={styles.panel} onClick={(event) => event.stopPropagation()}>
        <div className={styles.panelHeader}>
          <div>
            <span className={styles.eyebrow}>GAME CONCIERGE</span>
            <h2 id="settings-title" className={styles.panelTitle}>
              {lang === "ja" ? "設定" : "Settings"}
            </h2>
          </div>
          <button type="button" className={styles.closeBtn} onClick={onClose} aria-label="close">
            ✕
          </button>
        </div>

        <section className={styles.section}>
          <div className={styles.sectionHeading}>
            <h3>{lang === "ja" ? "推薦ユーザー" : "Recommendation user"}</h3>
            <p>
              {lang === "ja"
                ? "個人化に利用する評価・閲覧履歴のユーザーを選択します。"
                : "Choose whose ratings and viewing history are used for personalization."}
            </p>
          </div>
          <TestUserSelect userId={userId} onChange={onUserChange} />
        </section>

        <section className={styles.section}>
          <div className={styles.sectionHeading}>
            <h3>{lang === "ja" ? "表示・検証" : "Display and diagnostics"}</h3>
            <p>
              {lang === "ja"
                ? "発表時には不要な技術情報や比較機能をまとめています。"
                : "Technical details and comparison controls that are not needed in the presentation view."}
            </p>
          </div>

          <label className={styles.switchRow}>
            <span>
              <strong>{t.devView}</strong>
              <small>
                {lang === "ja" ? "検索意図や推薦根拠の詳細を表示" : "Show search intent and recommendation diagnostics"}
              </small>
            </span>
            <input
              type="checkbox"
              checked={devMode}
              onChange={(event) => onDevModeChange(event.target.checked)}
            />
          </label>

          {devMode && (
            <div className={styles.advanced}>
              <label className={styles.switchRow}>
                <span>
                  <strong>{t.generalMode}</strong>
                  <small>{t.generalModeDetail}</small>
                </span>
                <input
                  type="checkbox"
                  checked={generalMode}
                  onChange={(event) => onGeneralModeChange(event.target.checked)}
                />
              </label>

              <button
                type="button"
                className={styles.dangerButton}
                onClick={onClearHistory}
                disabled={clearingHistory || generalMode}
              >
                {clearingHistory
                  ? lang === "ja" ? "履歴を削除中…" : "Clearing history…"
                  : t.clearHistory}
              </button>
            </div>
          )}
        </section>
      </div>
    </div>
  );
}
