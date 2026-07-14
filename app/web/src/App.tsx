import { useState } from "react";
import { LangProvider } from "./i18n";
import ChatPage from "./pages/ChatPage";
import SplashScreen from "./components/SplashScreen";
import type { RecommendationTool } from "./types/tool";

// 起動画面で「個人化推薦」と「対話型推薦」を選択し、選んだツールだけを起動する。
// 選択後はChatPage内のツールスイッチャーで、起動画面を経由せずに切り替えられる。
export default function App() {
  const [showSplash, setShowSplash] = useState(true);
  const [heroEntrance, setHeroEntrance] = useState(false);
  const [selectedTool, setSelectedTool] = useState<RecommendationTool | null>(null);

  function start(tool: RecommendationTool) {
    setSelectedTool(tool);
    setHeroEntrance(tool === "dialogue");
  }

  return (
    <LangProvider>
      {showSplash && (
        <SplashScreen
          onStart={start}
          onDone={() => setShowSplash(false)}
        />
      )}
      {selectedTool && (
        <ChatPage
          selectedTool={selectedTool}
          onToolChange={setSelectedTool}
          heroEntrance={heroEntrance}
        />
      )}
    </LangProvider>
  );
}
