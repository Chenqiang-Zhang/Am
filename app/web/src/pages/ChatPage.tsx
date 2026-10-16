import { useState } from "react";
import { chat, ApiError } from "../api/client";
import type { ChatMessage, Recommendation } from "../types/recommend";
import { useI18n } from "../i18n";
import ChatBubble from "../components/ChatBubble";
import HeroIllustration from "../components/HeroIllustration";
import QuickReplies from "../components/QuickReplies";
import RecommendationList from "../components/RecommendationList";
import PageHeader, { type PageControls } from "../components/PageHeader";
import styles from "./ChatPage.module.css";

const LIMIT = 8;

type Status = "idle" | "loading" | "error";

interface Props extends PageControls {
  heroEntrance?: boolean;
}

export default function ChatPage({ heroEntrance = false, ...controls }: Props) {
  const { playerId, devMode } = controls;
  const { t, lang } = useI18n();
  const [conversation, setConversation] = useState<ChatMessage[]>([]);
  const [options, setOptions] = useState<string[]>([]);
  const [recommendations, setRecommendations] = useState<Recommendation[] | null>(null);
  const [status, setStatus] = useState<Status>("idle");
  const [error, setError] = useState<string | null>(null);
  const [input, setInput] = useState("");

  async function send(text: string) {
    const content = text.trim();
    if (!content || status === "loading") return;

    const next: ChatMessage[] = [...conversation, { role: "user", content }];
    setConversation(next);
    setInput("");
    setOptions([]);
    setRecommendations(null);
    setError(null);
    setStatus("loading");

    try {
      const r = await chat({ messages: next, player_id: playerId || null, limit: LIMIT, lang });
      setConversation([...next, { role: "assistant", content: r.reply }]);
      setOptions(r.options);
      setRecommendations(r.recommendations);
      setStatus("idle");
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Error");
      setStatus("error");
    }
  }

  function reset() {
    setConversation([]);
    setOptions([]);
    setRecommendations(null);
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
      <PageHeader {...controls} />

      <section className={styles.conversation}>
        {conversation.length === 0 && <HeroIllustration entering={heroEntrance} />}
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
        {conversation.length > 0 && (
          <button className={styles.reset} type="button" onClick={reset} disabled={loading}>
            {t.reset}
          </button>
        )}
      </form>

      {recommendations && (
        <section className={styles.results}>
          <RecommendationList items={recommendations} devMode={devMode} />
          {conversation.length > 0 && (
            <div className={styles.restartBanner}>
              <span className={styles.restartText}>{t.restartPrompt}</span>
              <button type="button" className={styles.restartBtn} onClick={reset}>
                {t.restart}
              </button>
            </div>
          )}
        </section>
      )}
    </div>
  );
}
