import { useEffect, useRef, useState } from "react";
import { chat, clearHistory, isAbortError, recommendHome, warmHomeCache } from "../api/client";
import type { ChatMessage, Recommendation, SearchIntent } from "../types/recommend";
import { formatApiError, useI18n, type Lang } from "../i18n";
import ChatBubble from "../components/ChatBubble";
import HeroIllustration from "../components/HeroIllustration";
import QuickReplies from "../components/QuickReplies";
import PreferenceSummary from "../components/PreferenceSummary";
import IntentPanel from "../components/IntentPanel";
import RecommendationList from "../components/RecommendationList";
import HelpModal from "../components/HelpModal";
import SettingsModal from "../components/SettingsModal";
import { useStoredTestUserId } from "../components/TestUserSelect";
import type { RecommendationTool } from "../types/tool";
import styles from "./ChatPage.module.css";

const LIMIT = 8;

interface Result {
  recommendations: Recommendation[];
  preference_summary: string[];
  intent: SearchIntent | null;
  searchId: string | null;
  fallback: boolean;
  provisional: boolean;
}

// 再取得中も前回の結果を表示し続けるので、どのユーザー向けの結果かを結果側に持たせる
// （ユーザー切替直後に、古い結果への閲覧ログ等を新しいユーザーへ付けないため）。
interface HomeResult extends Result {
  userId: string | null;
}

type Status = "idle" | "loading" | "error";

// 対話型推薦ページ。会話で希望を聞き取り → 十分になったら推薦する。
interface Props {
  selectedTool: RecommendationTool;
  onToolChange: (tool: RecommendationTool) => void;
  heroEntrance?: boolean;
}

export default function ChatPage({ selectedTool, onToolChange, heroEntrance = false }: Props) {
  const { t, lang, setLang } = useI18n();
  const [conversation, setConversation] = useState<ChatMessage[]>([]);
  const [options, setOptions] = useState<string[]>([]);
  const [dialogueResult, setDialogueResult] = useState<Result | null>(null);
  const [homeResult, setHomeResult] = useState<HomeResult | null>(null);
  const [status, setStatus] = useState<Status>("idle");
  // エラーは文言ではなく例外のまま持ち、描画時に formatApiError() で現在の言語にする。
  const [dialogueError, setDialogueError] = useState<unknown>(null);
  const [homeError, setHomeError] = useState<unknown>(null);
  // 送信中、または送信に失敗したユーザー発話。応答を受け取るまで conversation には
  // 確定させない（失敗後に次を送ってもuserターンが連続しないように）。失敗時は吹き出しを
  // 残したまま、再試行ボタンで同じ発話を送り直せる。
  const [pendingMessage, setPendingMessage] = useState<string | null>(null);
  // 進行中の /chat。最初からやり直す・ユーザー/一般モード切替・言語切替で取り消し、
  // 古い応答が新しい状態を上書きしないようにする。
  const chatRequestRef = useRef<AbortController | null>(null);
  const [input, setInput] = useState("");
  const [devMode, setDevMode] = useState(false);
  const [helpOpen, setHelpOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [userId, setUserId] = useStoredTestUserId();
  const [homeLoading, setHomeLoading] = useState(false);
  // 一般モード: 選択中のテストユーザーの履歴を一時的に無視し、非個人化（人気商品
  // フォールバック相当）の結果と比較できるようにする。ユーザー選択自体は変えない。
  const [generalMode, setGeneralMode] = useState(false);
  // 選択ユーザーは個人化推薦専用。対話型推薦には渡さず、会話条件だけで検索する。
  const personalizedUserId = generalMode ? null : userId;
  // reset()を「会話が既に空(0件)のとき」に押しても再取得が走るよう、conversation.lengthの
  // 変化だけに頼らず明示的に発火させるためのカウンタ。
  const [homeReloadKey, setHomeReloadKey] = useState(0);
  const [clearingHistory, setClearingHistory] = useState(false);

  // 個人化推薦ツールを開いているときだけホーム推薦を取得する。対話型推薦の結果とは
  // 状態を分け、片方のAPI応答がもう片方の表示を上書きしないようにする。
  // 条件が変わったら前回のリクエストは取り消す。再取得中も前回の結果は消さずに
  // 薄く表示しておき、一覧が一瞬空になってちらつかないようにする。
  useEffect(() => {
    if (selectedTool !== "personalized") return;
    const controller = new AbortController();
    const requestUserId = personalizedUserId;
    (async () => {
      setHomeLoading(true);
      setHomeError(null);
      try {
        const r = await recommendHome({ user_id: requestUserId, limit: LIMIT, lang }, controller.signal);
        if (controller.signal.aborted) return;
        setHomeResult({
          recommendations: r.recommendations,
          preference_summary: [],
          intent: r.intent,
          searchId: r.search_id,
          fallback: r.fallback,
          provisional: false,
          userId: requestUserId,
        });
      } catch (e) {
        if (controller.signal.aborted || isAbortError(e)) return;
        setHomeError(e);
      } finally {
        if (!controller.signal.aborted) setHomeLoading(false);
      }
    })();
    return () => {
      controller.abort();
      setHomeLoading(false);
    };
  }, [selectedTool, personalizedUserId, lang, homeReloadKey]);

  // タブを閉じる/バックグラウンドに回した時に、次回開いた時のためのホーム推薦を
  // バックエンド側で先読みキャッシュしておく（履歴が無い場合は高速パスで十分なので
  // バックエンド側で無視される）。
  useEffect(() => {
    const uid = personalizedUserId;
    function warm() {
      if (selectedTool !== "personalized") return;
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
  }, [selectedTool, personalizedUserId, lang]);

  // 画面を離れる時は応答待ちの /chat も取り消す。
  useEffect(() => () => chatRequestRef.current?.abort(), []);

  function cancelChat() {
    chatRequestRef.current?.abort();
    chatRequestRef.current = null;
  }

  // history（確定済みの会話）に content を足して1ターン送る。後から始まった送信や
  // cancelChat() で chatRequestRef が差し替わっていたら、その応答は捨てる。
  async function requestChat(history: ChatMessage[], content: string, requestLang: Lang) {
    cancelChat();
    const controller = new AbortController();
    chatRequestRef.current = controller;
    const next: ChatMessage[] = [...history, { role: "user", content }];
    setPendingMessage(content);
    setDialogueError(null);
    setStatus("loading");

    try {
      // 対話型推薦は選択ユーザーの評価・閲覧履歴を使わない。
      const r = await chat(next, LIMIT, requestLang, null, controller.signal);
      if (chatRequestRef.current !== controller) return;
      chatRequestRef.current = null;
      if (r.action === "ask") {
        setConversation([...next, { role: "assistant", content: r.question ?? "" }]);
        setOptions(r.options);
      } else {
        setConversation(next);
        setOptions([]);
      }
      setPendingMessage(null);
      setDialogueResult({
        recommendations: r.recommendations,
        preference_summary: r.preference_summary,
        intent: r.intent,
        searchId: r.search_id,
        fallback: r.fallback,
        provisional: r.provisional ?? r.action === "ask",
      });
      setStatus("idle");
    } catch (e) {
      if (chatRequestRef.current !== controller || isAbortError(e)) return;
      chatRequestRef.current = null;
      // pendingMessage は残す（吹き出し＋再試行ボタン）。conversation は送信前のまま。
      setDialogueError(e);
      setStatus("error");
    }
  }

  function send(text: string) {
    const content = text.trim();
    if (!content || status === "loading") return;
    setInput("");
    // 失敗した発話が残っていても、それは確定済みの会話に含めず新しい発話で置き換える。
    void requestChat(conversation, content, lang);
  }

  function retry() {
    if (pendingMessage === null || status === "loading") return;
    void requestChat(conversation, pendingMessage, lang);
  }

  // 応答待ちの間に言語が切り替わったら、古い言語の応答は捨てて同じ発話を新しい言語で
  // 送り直す（応答が届いていない間はまだ会話に確定していないので、送り直しても重複しない）。
  const lastLangRef = useRef(lang);
  useEffect(() => {
    if (lastLangRef.current === lang) return;
    lastLangRef.current = lang;
    if (chatRequestRef.current && pendingMessage !== null) {
      void requestChat(conversation, pendingMessage, lang);
    }
  }, [lang]);

  function resetDialogue() {
    cancelChat();
    setConversation([]);
    setOptions([]);
    setPendingMessage(null);
    setDialogueResult(null);
    setDialogueError(null);
    setInput("");
    setStatus("idle");
  }

  function refreshHome() {
    setHomeReloadKey((k) => k + 1);
  }

  function handleUserChange(nextUserId: string) {
    setUserId(nextUserId);
    resetDialogue();
  }

  function handleDevModeChange(enabled: boolean) {
    setDevMode(enabled);
    if (!enabled && generalMode) {
      setGeneralMode(false);
      resetDialogue();
    }
  }

  function handleGeneralModeChange(enabled: boolean) {
    setGeneralMode(enabled);
    resetDialogue();
  }

  // RATED（データセット由来の評価履歴）以外——VIEWED行動ログとSearchLog検索履歴——を
  // 選択中のユーザーについて削除する。個人化の根拠であるRATEDは削除されない。
  async function handleClearHistory() {
    // 一般モードは匿名操作であり、選択中ユーザーの履歴を操作してはならない。
    if (generalMode || clearingHistory || !window.confirm(t.clearHistoryConfirm)) return;
    setClearingHistory(true);
    try {
      await clearHistory(userId);
      resetDialogue();
      refreshHome();
      window.alert(t.clearHistoryDone);
    } catch {
      window.alert(t.clearHistoryFailed);
    } finally {
      setClearingHistory(false);
    }
  }

  function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    send(input);
  }

  const loading = status === "loading";
  const isPersonalized = selectedTool === "personalized";

  return (
    <div className={styles.page}>
      {helpOpen && <HelpModal onClose={() => setHelpOpen(false)} />}
      {settingsOpen && (
        <SettingsModal
          userId={userId}
          onUserChange={handleUserChange}
          devMode={devMode}
          onDevModeChange={handleDevModeChange}
          generalMode={generalMode}
          onGeneralModeChange={handleGeneralModeChange}
          clearingHistory={clearingHistory}
          onClearHistory={handleClearHistory}
          onClose={() => setSettingsOpen(false)}
        />
      )}
      <header className={styles.header}>
        <div className={styles.headerControls}>
          <nav className={styles.toolSwitcher} aria-label={t.toolSwitcherLabel}>
            <button
              type="button"
              className={isPersonalized ? styles.toolActive : styles.toolButton}
              onClick={() => onToolChange("personalized")}
              aria-current={isPersonalized ? "page" : undefined}
            >
              <strong>{t.personalizedTool}</strong>
            </button>
            <button
              type="button"
              className={!isPersonalized ? styles.toolActive : styles.toolButton}
              onClick={() => onToolChange("dialogue")}
              aria-current={!isPersonalized ? "page" : undefined}
            >
              <strong>{t.dialogueTool}</strong>
            </button>
          </nav>
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
          <button
            type="button"
            className={styles.iconBtn}
            onClick={() => setHelpOpen(true)}
            aria-label={lang === "ja" ? "使い方" : "How to use"}
            title={lang === "ja" ? "使い方" : "How to use"}
          >
            <span aria-hidden="true">?</span>
          </button>
          <button
            type="button"
            className={styles.iconBtn}
            onClick={() => setSettingsOpen(true)}
            aria-label={lang === "ja" ? "設定" : "Settings"}
            title={lang === "ja" ? "設定" : "Settings"}
          >
            <svg viewBox="0 0 24 24" aria-hidden="true">
              <path d="M12 8.6a3.4 3.4 0 1 0 0 6.8 3.4 3.4 0 0 0 0-6.8Zm8.1 4.7-1.7 1a7 7 0 0 1-.7 1.7l.5 1.9-2.3 2.3-1.9-.5a7 7 0 0 1-1.7.7l-1 1.7H8.7l-1-1.7a7 7 0 0 1-1.7-.7l-1.9.5-2.3-2.3.5-1.9a7 7 0 0 1-.7-1.7l-1.7-1v-2.6l1.7-1a7 7 0 0 1 .7-1.7l-.5-1.9 2.3-2.3 1.9.5a7 7 0 0 1 1.7-.7l1-1.7h2.6l1 1.7a7 7 0 0 1 1.7.7l1.9-.5 2.3 2.3-.5 1.9a7 7 0 0 1 .7 1.7l1.7 1v2.6Z" />
            </svg>
          </button>
        </div>
        <div className={styles.headerText}>
          <h1 className={styles.title}>{isPersonalized ? t.personalizedTitle : t.dialogueTitle}</h1>
          <p className={styles.subtitle}>{isPersonalized ? t.personalizedSubtitle : t.dialogueSubtitle}</p>
          {devMode && (
            <div
              className={`${styles.modeStatus} ${generalMode ? styles.modeStatusGeneral : styles.modeStatusPersonalized}`}
              role="status"
            >
              <span className={styles.modeDot} aria-hidden="true" />
              <span className={styles.modeCopy}>
                <strong>{generalMode ? t.generalMode : t.personalizedMode}</strong>
                <span>{generalMode ? t.generalModeDetail : t.personalizedModeDetail}</span>
              </span>
            </div>
          )}
        </div>
      </header>

      {isPersonalized ? (
        <>
          <section className={styles.personalizedIntro}>
            <div>
              <span className={styles.toolKicker}>{t.personalizedTool}</span>
              <p>{t.personalizedLead}</p>
            </div>
            <button type="button" onClick={refreshHome} disabled={homeLoading}>
              {homeLoading && homeResult ? t.homeRefreshing : t.refreshRecommendations}
            </button>
          </section>

          {homeError != null && (
            <div className={styles.error} role="alert">
              {formatApiError(homeError, t)}
            </div>
          )}

          {homeLoading && !homeResult && (
            <p className={styles.initialLoading}>
              {lang === "ja" ? "あなた向けのおすすめを読み込み中…" : "Loading your recommendations…"}
            </p>
          )}

          {homeResult && (
            <section
              className={homeLoading ? `${styles.results} ${styles.stale}` : styles.results}
              aria-busy={homeLoading}
            >
              {devMode && homeResult.intent && <IntentPanel intent={homeResult.intent} />}
              <RecommendationList
                items={homeResult.recommendations}
                devMode={devMode}
                userId={homeResult.userId}
                searchId={homeResult.searchId}
                fallback={homeResult.fallback}
              />
            </section>
          )}
        </>
      ) : (
        <>
          <section className={styles.conversation}>
            {conversation.length === 0 && pendingMessage === null && (
              <HeroIllustration entering={heroEntrance} />
            )}
            <ChatBubble role="assistant" content={t.greeting} />
            {conversation.map((m, i) => (
              <ChatBubble key={i} role={m.role} content={m.content} />
            ))}
            {pendingMessage !== null && <ChatBubble role="user" content={pendingMessage} />}
            {loading && <ChatBubble role="assistant" content="" loading />}
            {dialogueError != null && (
              <div className={styles.error} role="alert">
                {formatApiError(dialogueError, t)}
                {pendingMessage !== null && (
                  <button type="button" className={styles.retryBtn} onClick={retry}>
                    {t.retry}
                  </button>
                )}
              </div>
            )}
            {!loading && options.length > 0 && (
              <QuickReplies options={options} onPick={send} allowOther disabled={loading} />
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
            {/* 応答待ちでも押せる（resetDialogue が進行中の /chat を取り消す）。 */}
            {(conversation.length > 0 || pendingMessage !== null) && (
              <button className={styles.reset} type="button" onClick={resetDialogue}>
                {t.reset}
              </button>
            )}
          </form>

          {dialogueResult && (
            <section className={styles.results}>
              <PreferenceSummary items={dialogueResult.preference_summary} />
              {devMode && dialogueResult.intent && <IntentPanel intent={dialogueResult.intent} />}
              <RecommendationList
                items={dialogueResult.recommendations}
                devMode={devMode}
                userId={null}
                searchId={dialogueResult.searchId}
                fallback={dialogueResult.fallback}
                provisional={dialogueResult.provisional}
              />
              {!dialogueResult.provisional && conversation.length > 0 && (
                <div className={styles.restartBanner}>
                  <span className={styles.restartText}>
                    {lang === "ja" ? "他の商品を探しますか？" : "Looking for something else?"}
                  </span>
                  <button type="button" className={styles.restartBtn} onClick={resetDialogue}>
                    {lang === "ja" ? "最初からやり直す" : "Start over"}
                  </button>
                </div>
              )}
            </section>
          )}
        </>
      )}
    </div>
  );
}
