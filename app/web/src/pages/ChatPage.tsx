import { useEffect, useState } from "react";
import { chat, recommendHome, warmHomeCache, ApiError } from "../api/client";
import type { ChatMessage, Recommendation, SearchIntent } from "../types/recommend";
import { useI18n } from "../i18n";
import ChatBubble from "../components/ChatBubble";
import QuickReplies from "../components/QuickReplies";
import PreferenceSummary from "../components/PreferenceSummary";
import IntentPanel from "../components/IntentPanel";
import RecommendationList from "../components/RecommendationList";
import HelpModal from "../components/HelpModal";
import TestUserSelect, { useStoredTestUserId } from "../components/TestUserSelect";
import styles from "./ChatPage.module.css";

const LIMIT = 8;

interface Result {
  recommendations: Recommendation[];
  preference_summary: string[];
  intent: SearchIntent | null;
  searchId: string | null;
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
  const [userId, setUserId] = useStoredTestUserId();
  const [initialLoading, setInitialLoading] = useState(false);
  // 一般モード: 選択中のテストユーザーの履歴を一時的に無視し、非個人化（人気商品
  // フォールバック相当）の結果と比較できるようにする。ユーザー選択自体は変えない。
  const [generalMode, setGeneralMode] = useState(false);
  const effectiveUserId = generalMode ? null : userId;
  // reset()を「会話が既に空(0件)のとき」に押しても再取得が走るよう、conversation.lengthの
  // 変化だけに頼らず明示的に発火させるためのカウンタ。
  const [reloadKey, setReloadKey] = useState(0);

  // 会話を始める前は、ひとまず何かしらのおすすめを表示しておく（履歴ベース、
  // 履歴が無ければrecommendHome()内部で人気商品にフォールバックする）。
  // これはユーザーの発話に対する応答ではないので、チャットの「入力中...」表示
  // (status/loading)とは別のローディング状態にする — 会話が勝手に動いたように
  // 見えないようにするため。
  useEffect(() => {
    if (conversation.length > 0) return;
    let cancelled = false;
    (async () => {
      setInitialLoading(true);
      setError(null);
      try {
        const r = await recommendHome({ user_id: effectiveUserId, limit: LIMIT, lang });
        if (cancelled) return;
        setResult({
          recommendations: r.recommendations,
          preference_summary: [],
          intent: r.intent,
          searchId: r.search_id,
        });
      } catch (e) {
        if (cancelled) return;
        setError(e instanceof ApiError ? e.message : "Error");
      } finally {
        if (!cancelled) setInitialLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [conversation.length, effectiveUserId, lang, reloadKey]);

  // タブを閉じる/バックグラウンドに回した時に、次回開いた時のためのホーム推薦を
  // バックエンド側で先読みキャッシュしておく（履歴が無い場合は高速パスで十分なので
  // バックエンド側で無視される）。
  useEffect(() => {
    const uid = effectiveUserId;
    function warm() {
      warmHomeCache({ user_id: uid, limit: LIMIT, lang });
    }
    function onVisibilityChange() {
      if (document.visibilityState === "hidden") warm();
    }
    document.addEventListener("visibilitychange", onVisibilityChange);
    window.addEventListener("pagehide", warm);
    return () => {
      document.removeEventListener("visibilitychange", onVisibilityChange);
      window.removeEventListener("pagehide", warm);
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
          searchId: r.search_id,
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
    setReloadKey((k) => k + 1);
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
            {t.generalMode}
          </label>
          <TestUserSelect userId={userId} onChange={setUserId} />
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
        {error && (
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

      {initialLoading && !result && (
        <p className={styles.initialLoading}>{lang === "ja" ? "おすすめを読み込み中…" : "Loading recommendations…"}</p>
      )}

      {result && (
        <section className={styles.results}>
          <PreferenceSummary items={result.preference_summary} />
          {devMode && result.intent && <IntentPanel intent={result.intent} />}
          <RecommendationList
            items={result.recommendations}
            devMode={devMode}
            userId={userId}
            searchId={result.searchId}
          />
          {conversation.length > 0 && (
            <div className={styles.restartBanner}>
              <span className={styles.restartText}>
                {lang === "ja" ? "他の商品を探しますか？" : "Looking for something else?"}
              </span>
              <button type="button" className={styles.restartBtn} onClick={reset}>
                {lang === "ja" ? "最初からやり直す" : "Start over"}
              </button>
            </div>
          )}
        </section>
      )}
    </div>
  );
}
