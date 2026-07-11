import { useState } from "react";
import { LangProvider } from "./i18n";
import ChatPage from "./pages/ChatPage";
import SplashScreen from "./components/SplashScreen";

// v2: 対話型推薦(CRS)を主役にする。言語切替(日本語/English)を LangProvider で全体に提供。
// SplashScreenはChatPageと同時にマウントし、裏でChatPage側の初回データ取得を進めつつ、
// 起動アニメーションの最低表示時間だけ上に被せておく(表示が終わる頃には裏の準備も進んでいる)。
export default function App() {
  const [showSplash, setShowSplash] = useState(true);
  return (
    <LangProvider>
      {showSplash && <SplashScreen onDone={() => setShowSplash(false)} />}
      <ChatPage />
    </LangProvider>
  );
}
