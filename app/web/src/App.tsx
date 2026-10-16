import { useState } from "react";
import { LangProvider } from "./i18n";
import ChatPage from "./pages/ChatPage";
import SplashScreen from "./components/SplashScreen";

export default function App() {
  const [showSplash, setShowSplash] = useState(true);
  const [heroEntrance, setHeroEntrance] = useState(false);
  return (
    <LangProvider>
      {showSplash && (
        <SplashScreen
          onStart={() => setHeroEntrance(true)}
          onDone={() => setShowSplash(false)}
        />
      )}
      <ChatPage heroEntrance={heroEntrance} />
    </LangProvider>
  );
}
