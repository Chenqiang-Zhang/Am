import { useState } from "react";
import { LangProvider } from "./i18n";
import ChatPage from "./pages/ChatPage";
import DialoguePage from "./pages/DialoguePage";
import RecommendPage from "./pages/RecommendPage";
import SplashScreen from "./components/SplashScreen";
import PageHeader, { type PageControls, type Tab } from "./components/PageHeader";
import styles from "./App.module.css";
import { useStoredPlayerId } from "./components/TestUserSelect";

const TABS: Tab[] = ["chat", "dialogue", "recommend"];

// A link with #dialogue (or #chat, #recommend) opens that tab after the splash screen; ?nosplash skips the
// splash (for screenshots and quick reloads while developing).
function tabFromHash(): Tab | null {
  const hash = window.location.hash.slice(1);
  return TABS.includes(hash as Tab) ? (hash as Tab) : null;
}

const SKIP_SPLASH = new URLSearchParams(window.location.search).has("nosplash");

export default function App() {
  const linked = tabFromHash();
  const [showSplash, setShowSplash] = useState(!SKIP_SPLASH);
  const [heroEntrance, setHeroEntrance] = useState(false);
  const [tab, setTab] = useState<Tab>(linked ?? "chat");
  const [devMode, setDevMode] = useState(false);
  const [playerId, setPlayerId] = useStoredPlayerId();

  const controls: PageControls = {
    tab,
    onTabChange: setTab,
    devMode,
    onDevModeChange: setDevMode,
    playerId,
    onPlayerChange: setPlayerId,
  };

  return (
    <LangProvider>
      {showSplash && (
        <SplashScreen
          onStart={() => setHeroEntrance(true)}
          onDone={() => setShowSplash(false)}
        />
      )}
      {/* One header for every tab, in a frame of fixed width, so its controls never move between tabs. */}
      <div className={styles.headerFrame}>
        <PageHeader {...controls} />
      </div>
      {tab === "chat" && <ChatPage heroEntrance={heroEntrance} {...controls} />}
      {tab === "dialogue" && <DialoguePage {...controls} />}
      {tab === "recommend" && <RecommendPage {...controls} />}
    </LangProvider>
  );
}
