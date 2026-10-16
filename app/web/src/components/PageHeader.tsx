import { useState } from "react";
import { useI18n } from "../i18n";
import HelpModal from "./HelpModal";
import TestUserSelect from "./TestUserSelect";
import styles from "./PageHeader.module.css";

export type Tab = "chat" | "recommend";

export interface PageControls {
  tab: Tab;
  onTabChange: (tab: Tab) => void;
  devMode: boolean;
  onDevModeChange: (devMode: boolean) => void;
  playerId: string;
  onPlayerChange: (playerId: string) => void;
}

export default function PageHeader({
  tab,
  onTabChange,
  devMode,
  onDevModeChange,
  playerId,
  onPlayerChange,
}: PageControls) {
  const { t, lang, setLang } = useI18n();
  const [helpOpen, setHelpOpen] = useState(false);

  return (
    <header className={styles.header}>
      {helpOpen && <HelpModal onClose={() => setHelpOpen(false)} />}
      <div className={styles.headerControls}>
        <button type="button" className={styles.helpBtn} onClick={() => setHelpOpen(true)} aria-label="help">
          ?
        </button>
        <div className={styles.segmented}>
          <button
            type="button"
            className={lang === "ja" ? styles.segmentActive : styles.segment}
            onClick={() => setLang("ja")}
          >
            日本語
          </button>
          <button
            type="button"
            className={lang === "en" ? styles.segmentActive : styles.segment}
            onClick={() => setLang("en")}
          >
            EN
          </button>
        </div>
        <label className={styles.devToggle}>
          <input type="checkbox" checked={devMode} onChange={(e) => onDevModeChange(e.target.checked)} />
          {t.devView}
        </label>
        <TestUserSelect playerId={playerId} onChange={onPlayerChange} />
      </div>
      <div className={styles.headerText}>
        <h1 className={styles.title}>{t.appTitle}</h1>
      </div>
      <div className={styles.tabs}>
        <div className={styles.segmented}>
          <button
            type="button"
            className={tab === "chat" ? styles.segmentActive : styles.segment}
            onClick={() => onTabChange("chat")}
          >
            {t.tabChat}
          </button>
          <button
            type="button"
            className={tab === "recommend" ? styles.segmentActive : styles.segment}
            onClick={() => onTabChange("recommend")}
          >
            {t.tabRecommend}
          </button>
        </div>
      </div>
    </header>
  );
}
