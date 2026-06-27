import { LangProvider } from "./i18n";
import ChatPage from "./pages/ChatPage";

// v2: 対話型推薦（CRS）を主役にする。言語切替（日本語/English）を LangProvider で全体に提供。
export default function App() {
  return (
    <LangProvider>
      <ChatPage />
    </LangProvider>
  );
}
