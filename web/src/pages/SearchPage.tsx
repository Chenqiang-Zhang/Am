import { useState } from "react";
import { recommend } from "../api/client";
import type { RecommendResponse } from "../types/recommend";
import SearchBar from "../components/SearchBar";
import IntentPanel from "../components/IntentPanel";
import RecommendationList from "../components/RecommendationList";
import { useI18n } from "../i18n";
import { useUserIdentity } from "../lib/userIdentity";
import styles from "./SearchPage.module.css";

type Status = "idle" | "loading" | "success" | "error";

// 画面全体の状態管理・API 呼び出しはこのページに集約する。
// components/ 配下は props を受けて描画するだけ（ロジックを持たない）。
export default function SearchPage() {
  const { lang } = useI18n();
  const [status, setStatus] = useState<Status>("idle");
  const [response, setResponse] = useState<RecommendResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  // 再試行のために直近の検索条件を保持する
  const [lastQuery, setLastQuery] = useState<{ query: string; limit: number } | null>(null);
  // 開発者ビュー（Intent・スコア内訳など機械的な根拠を表示）。既定はユーザー向けにOFF。
  const [devMode, setDevMode] = useState(false);
  const { userId } = useUserIdentity();

  async function runSearch(query: string, limit: number) {
    setStatus("loading");
    setError(null);
    setLastQuery({ query, limit });
    try {
      const data = await recommend({ query, limit, lang, user_id: userId });
      setResponse(data);
      setStatus("success");
    } catch (e) {
      setError(e instanceof Error ? e.message : "不明なエラーが発生しました");
      setStatus("error");
    }
  }

  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <div className={styles.headerText}>
          <h1 className={styles.title}>商品レコメンド</h1>
          <p className={styles.subtitle}>
            探している商品を、自然な言葉で入力してください。
          </p>
        </div>
        <label className={styles.devToggle} title="システムの解釈・スコア内訳など開発者向けの詳細を表示">
          <input
            type="checkbox"
            checked={devMode}
            onChange={(e) => setDevMode(e.target.checked)}
          />
          開発者ビュー
        </label>
      </header>

      <SearchBar onSearch={runSearch} loading={status === "loading"} />

      <main className={styles.main}>
        {status === "idle" && (
          <div className={styles.placeholder}>
            上の入力欄に探している商品の条件を入力するか、例クエリを選んでください。
            <br />
            推薦結果には「なぜ推薦されたか」の根拠（一致した属性・特徴文・スコア内訳）が付きます。
          </div>
        )}

        {status === "loading" && (
          <div className={styles.placeholder}>
            <span className={styles.spinner} aria-hidden="true" />
            検索中…（クエリの意図抽出に LLM を使うため数秒かかります）
          </div>
        )}

        {status === "error" && (
          <div className={styles.errorBox} role="alert">
            <strong>検索に失敗しました</strong>
            <p className={styles.errorMessage}>{error}</p>
            {lastQuery && (
              <button
                className={styles.retryButton}
                onClick={() => runSearch(lastQuery.query, lastQuery.limit)}
              >
                再試行
              </button>
            )}
          </div>
        )}

        {status === "success" && response && (
          <>
            {devMode && <IntentPanel intent={response.intent} />}
            <RecommendationList
              items={response.recommendations}
              devMode={devMode}
              userId={userId}
              query={response.query}
              source="search"
            />
          </>
        )}
      </main>
    </div>
  );
}
