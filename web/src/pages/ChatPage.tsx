import { useState } from "react";
import { chat, ApiError } from "../api/client";
import type { ChatMessage, Recommendation, SearchIntent } from "../types/recommend";
import { useI18n } from "../i18n";
import ChatBubble from "../components/ChatBubble";
import QuickReplies from "../components/QuickReplies";
import PreferenceSummary from "../components/PreferenceSummary";
import IntentPanel from "../components/IntentPanel";
import RecommendationList from "../components/RecommendationList";
import HelpModal from "../components/HelpModal";
import styles from "./ChatPage.module.css";

const LIMIT = 8;

interface Result {
  recommendations: Recommendation[];
  preference_summary: string[];
  intent: SearchIntent | null;
}

type Status = "idle" | "loading" | "error";

// 対話型推薦ページ。会話で希望を聞き取り → 十分になったら推薦する。
export default function ChatPage() {
  const { t, lang, setLang } = useI18n();
  const [conversation, setConversation] = useState<ChatMessage[]>([]);
  const [options, setOptions] = useState<string[]>([]);
  const [result, setResult] = useState<Result | null>(null);
  const [status, setStatus] = useState<Status>("idle");
  const [error, setError] = useState<string | null>(null);
  const [input, setInput] = useState("");
  const [devMode, setDevMode] = useState(false);
  const [helpOpen, setHelpOpen] = useState(false);

  async function send(text: string) {
    const content = text.trim();
    if (!content || status === "loading") return;

    const next: ChatMessage[] = [...conversation, { role: "user", content }];
    setConversation(next);
    setInput("");
    setOptions([]);
    setResult(null);
    setError(null);
    setStatus("loading");

    try {
      const r = await chat(next, LIMIT, lang);
      if (r.action === "ask") {
        setConversation([...next, { role: "assistant", content: r.question ?? "" }]);
        setOptions(r.options);
      } else {
        setResult({
          recommendations: r.recommendations,
          preference_summary: r.preference_summary,
          intent: r.intent,
        });
      }
      setStatus("idle");
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Error");
      setStatus("error");
    }
  }

  function reset() {
    setConversation([]);
    setOptions([]);
    setResult(null);
    setError(null);
    setInput("");
    setStatus("idle");
  }

  function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    send(input);
  }

  const loading = status === "loading";

  return (
    <div className={styles.page}>
      {helpOpen && <HelpModal onClose={() => setHelpOpen(false)} />}
      <header className={styles.header}>
        <div className={styles.headerControls}>
          <button
            type="button"
            className={styles.helpBtn}
            onClick={() => setHelpOpen(true)}
            aria-label="使い方"
          >
            ?
          </button>
          <div className={styles.langToggle}>
            <button
              type="button"
              className={lang === "ja" ? styles.langActive : styles.langBtn}
              onClick={() => setLang("ja")}
            >
              日本語
            </button>
            <button
              type="button"
              className={lang === "en" ? styles.langActive : styles.langBtn}
              onClick={() => setLang("en")}
            >
              EN
            </button>
          </div>
          <label className={styles.devToggle}>
            <input
              type="checkbox"
              checked={devMode}
              onChange={(e) => setDevMode(e.target.checked)}
            />
            {t.devView}
          </label>
        </div>
        <div className={styles.headerText}>
          <h1 className={styles.title}>{t.appTitle}</h1>
          <p className={styles.subtitle}>{t.appSubtitle}</p>
        </div>
      </header>

      <section className={styles.conversation}>
        <ChatBubble role="assistant" content={t.greeting} />
        {conversation.map((m, i) => (
          <ChatBubble key={i} role={m.role} content={m.content} />
        ))}
        {loading && <ChatBubble role="assistant" content="" loading />}
        {!loading && options.length > 0 && (
          <QuickReplies options={options} onPick={send} allowOther disabled={loading} />
        )}
        {status === "error" && error && (
          <div className={styles.error} role="alert">
            {error}
          </div>
        )}
      </section>

      <form className={styles.inputBar} onSubmit={onSubmit}>
        <input
          className={styles.input}
          type="text"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder={t.inputPlaceholder}
          aria-label="message"
          disabled={loading}
        />
        <button className={styles.send} type="submit" disabled={loading || !input.trim()}>
          {t.send}
        </button>
        <button
          className={styles.omakase}
          type="button"
          onClick={() => send(lang === "ja" ? "特にこだわりはありません。おまかせで。" : "No particular preference. Surprise me.")}
          disabled={loading}
        >
          {t.omakase}
        </button>
        {conversation.length > 0 && (
          <button className={styles.reset} type="button" onClick={reset} disabled={loading}>
            {t.reset}
          </button>
        )}
      </form>

      {result && (
        <section className={styles.results}>
          <PreferenceSummary items={result.preference_summary} />
          {devMode && result.intent && <IntentPanel intent={result.intent} />}
          <RecommendationList items={result.recommendations} devMode={devMode} />
          <div className={styles.restartBanner}>
            <span className={styles.restartText}>
              {lang === "ja" ? "他の商品を探しますか？" : "Looking for something else?"}
            </span>
            <button type="button" className={styles.restartBtn} onClick={reset}>
              {lang === "ja" ? "最初からやり直す" : "Start over"}
            </button>
          </div>
        </section>
      )}
    </div>
  );
}
