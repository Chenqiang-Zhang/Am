import { useEffect, useState } from "react";
import { chat, recommendHome, ApiError } from "../api/client";
import type { ChatMessage, Recommendation, SearchIntent } from "../types/recommend";
import { useI18n } from "../i18n";
import ChatBubble from "../components/ChatBubble";
import QuickReplies from "../components/QuickReplies";
import PreferenceSummary from "../components/PreferenceSummary";
import IntentPanel from "../components/IntentPanel";
import RecommendationList from "../components/RecommendationList";
import HelpModal from "../components/HelpModal";
import { useUserIdentity } from "../lib/userIdentity";
import { trackBehavior } from "../lib/behavior";
import styles from "./ChatPage.module.css";

const LIMIT = 8;

interface Result {
  recommendations: Recommendation[];
  preference_summary: string[];
  intent: SearchIntent | null;
  query: string;
}

type Status = "idle" | "loading" | "error";

// 対話型推薦ページ。会話で希望を聞き取り → 十分になったら推薦する。
export default function ChatPage() {
  const { t, lang, setLang } = useI18n();
  const [conversation, setConversation] = useState<ChatMessage[]>([]);
  const [options, setOptions] = useState<string[]>([]);
  const [result, setResult] = useState<Result | null>(null);
  const [homeResult, setHomeResult] = useState<Result | null>(null);
  const [homeStatus, setHomeStatus] = useState<"idle" | "loading" | "done" | "error">("idle");
  const [status, setStatus] = useState<Status>("idle");
  const [error, setError] = useState<string | null>(null);
  const [input, setInput] = useState("");
  const [devMode, setDevMode] = useState(false);
  const [helpOpen, setHelpOpen] = useState(false);
  const { userId, setUserId, resetUserId } = useUserIdentity();
  const [generalMode, setGeneralMode] = useState(false);
  const effectiveUserId = generalMode ? null : userId;

  useEffect(() => {
    let cancelled = false;
    async function loadHome() {
      setHomeStatus("loading");
      try {
        const data = await recommendHome({ user_id: effectiveUserId, limit: LIMIT, lang });
        if (cancelled) return;
        setHomeResult({
          recommendations: data.recommendations,
          preference_summary: [],
          intent: data.intent,
          query: data.query,
        });
        setHomeStatus("done");
      } catch {
        if (!cancelled) setHomeStatus("error");
      }
    }
    loadHome();
    return () => {
      cancelled = true;
    };
  }, [effectiveUserId, lang]);

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
      const r = await chat(next, LIMIT, lang, effectiveUserId);
      if (r.action === "ask") {
        setConversation([...next, { role: "assistant", content: r.question ?? "" }]);
        setOptions(r.options);
      } else {
        setResult({
          recommendations: r.recommendations,
          preference_summary: r.preference_summary,
          intent: r.intent,
          query: next.filter((m) => m.role === "user").map((m) => m.content).join(" "),
        });
      }
      setStatus("idle");
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Error");
      setStatus("error");
    }
  }

  function reset() {
    if (conversation.length > 0) {
      trackBehavior({ userId, eventType: "restart", source: "chat" });
    }
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
          <label className={styles.devToggle}>
            <input
              type="checkbox"
              checked={generalMode}
              onChange={(e) => setGeneralMode(e.target.checked)}
            />
            {lang === "ja" ? "汎用モード" : "General"}
          </label>
          {!generalMode && (
            <label className={styles.userControl}>
              <span>{lang === "ja" ? "ユーザー" : "User"}</span>
              <input
                value={userId}
                onChange={(e) => setUserId(e.target.value)}
                aria-label="user id"
              />
              <button type="button" onClick={resetUserId} title={lang === "ja" ? "匿名IDを再生成" : "Reset anonymous ID"}>
                ↻
              </button>
            </label>
          )}
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
        {conversation.length > 0 && (
          <button className={styles.reset} type="button" onClick={reset} disabled={loading}>
            {t.reset}
          </button>
        )}
      </form>

      {!result && conversation.length === 0 && (
        <section className={styles.homePanel}>
          <div className={styles.homeHeader}>
            <div>
              <h2 className={styles.homeTitle}>
                {lang === "ja" ? "あなたへのおすすめ" : "For you"}
              </h2>
              <p className={styles.homeSubtitle}>
                {lang === "ja"
                  ? "行動履歴がある場合は好みに近い商品を、初回は高品質な人気商品を表示します。"
                  : "Personalized from your activity when available, otherwise high-quality popular products."}
              </p>
            </div>
            <button
              type="button"
              className={styles.homeRefresh}
              onClick={() => {
                setHomeResult(null);
                setHomeStatus("idle");
                void recommendHome({ user_id: userId, limit: LIMIT, lang })
                  .then((data) => {
                    setHomeResult({
                      recommendations: data.recommendations,
                      preference_summary: [],
                      intent: data.intent,
                      query: data.query,
                    });
                    setHomeStatus("done");
                  })
                  .catch(() => setHomeStatus("error"));
              }}
              disabled={homeStatus === "loading"}
            >
              {lang === "ja" ? "更新" : "Refresh"}
            </button>
          </div>
          {homeStatus === "loading" && (
            <p className={styles.homeState}>{lang === "ja" ? "読み込み中…" : "Loading recommendations..."}</p>
          )}
          {homeStatus === "error" && (
            <p className={styles.homeState}>{lang === "ja" ? "ホーム推薦を取得できませんでした。" : "Could not load home recommendations."}</p>
          )}
          {homeResult && homeResult.recommendations.length > 0 && (
            <>
              {devMode && homeResult.intent && <IntentPanel intent={homeResult.intent} />}
              <RecommendationList
                items={homeResult.recommendations}
                devMode={devMode}
                userId={userId}
                query={homeResult.query}
                source="home"
              />
            </>
          )}
        </section>
      )}

      {result && (
        <section className={styles.results}>
          <PreferenceSummary items={result.preference_summary} />
          {devMode && result.intent && <IntentPanel intent={result.intent} />}
          <RecommendationList
            items={result.recommendations}
            devMode={devMode}
            userId={userId}
            query={result.query}
            source="chat"
          />
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
