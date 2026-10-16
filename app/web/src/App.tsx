import { useState } from "react";
import { LangProvider } from "./i18n";
import ChatPage from "./pages/ChatPage";
import RecommendPage from "./pages/RecommendPage";
import SplashScreen from "./components/SplashScreen";
import type { PageControls, Tab } from "./components/PageHeader";
import { useStoredPlayerId } from "./components/TestUserSelect";

export default function App() {
  const [showSplash, setShowSplash] = useState(true);
  const [heroEntrance, setHeroEntrance] = useState(false);
  const [tab, setTab] = useState<Tab>("chat");
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
      {tab === "chat" ? (
        <ChatPage heroEntrance={heroEntrance} {...controls} />
      ) : (
        <RecommendPage {...controls} />
      )}
    </LangProvider>
  );
}
