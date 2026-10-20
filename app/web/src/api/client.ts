// ============================================================================
// API 通信の隔離層
//   fetch をここに閉じ込め、画面側は client.recommend(...) だけを呼ぶ。
//   接続先の変更・エラー処理の方針はこのファイルに集約する。
// ============================================================================
import type {
  ChatMessage,
  ChatResponse,
  ClearHistoryResponse,
  DescriptionResponse,
  FeedbackResponse,
  HomeRecommendRequest,
  RecommendRequest,
  RecommendResponse,
  ReviewsResponse,
  SampleUsersResponse,
  ViewLogRequest,
} from "../types/recommend";

// dev は vite.config.ts の proxy 経由で :8000 へ届く（CORS 不要）。
const BASE = "/api";
const CHAT_TIMEOUT_MS = 45_000;

/**
 * 失敗の種類。表示文言はここでは決めず、画面側が i18n.tsx の formatApiError() で
 * 現在の言語に変換する（言語を切り替えると表示中のエラーも追従する）。
 *   network: バックエンドに到達できない / timeout: 応答待ちが上限を超えた /
 *   aborted: 呼び出し側が signal で取り消した（画面には出さない） /
 *   http: 2xx以外のステータス / invalid_response: 応答がJSONとして読めない
 */
export type ApiErrorKind = "network" | "timeout" | "aborted" | "http" | "invalid_response";

/** API 呼び出しの失敗を表す例外（画面側で kind/status を出し分けられるよう保持） */
export class ApiError extends Error {
  kind: ApiErrorKind;
  status: number;
  /** FastAPI の detail を1行の文字列にしたもの（無ければ null） */
  detail: string | null;
  constructor(kind: ApiErrorKind, status = 0, detail: string | null = null) {
    // message は開発者向け（ログ用）。利用者向けの文言は formatApiError() を使う。
    super(`${kind}${status ? ` (${status})` : ""}${detail ? `: ${detail}` : ""}`);
    this.name = "ApiError";
    this.kind = kind;
    this.status = status;
    this.detail = detail;
  }
}

/** 呼び出し側の取り消し（signal.abort()）による失敗か。画面側はこれを無視する。 */
export function isAbortError(error: unknown): boolean {
  return error instanceof ApiError && error.kind === "aborted";
}

/**
 * FastAPI の detail を表示用の文字列にする。HTTPException は文字列（またはdict）だが、
 * バリデーションエラー(422)は [{loc, msg, type}, ...] の配列で返る。
 */
function formatDetail(detail: unknown): string | null {
  if (detail == null || detail === "") return null;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    const parts = detail.map((item) => {
      if (!item || typeof item !== "object") return String(item);
      const { loc, msg } = item as { loc?: unknown; msg?: unknown };
      const text = typeof msg === "string" ? msg : JSON.stringify(item);
      // loc の先頭は "body" / "query" などの場所名なので省き、フィールド名だけ残す。
      const field = Array.isArray(loc) ? loc.slice(1).join(".") : "";
      return field ? `${field}: ${text}` : text;
    });
    return parts.length > 0 ? parts.join("; ") : null;
  }
  return JSON.stringify(detail);
}

async function readDetail(res: Response): Promise<string | null> {
  try {
    const body = (await res.json()) as { detail?: unknown };
    return formatDetail(body?.detail);
  } catch {
    return null; /* JSON でないレスポンスは無視 */
  }
}

interface RequestOptions {
  /** 呼び出し側からの取り消し（新しいリクエストで古いものを捨てる時など） */
  signal?: AbortSignal;
  /** 応答本文の読み込みまで含めた待ち時間の上限 */
  timeoutMs?: number;
}

/**
 * fetch → ステータス確認 → JSON 解析までを1か所にまとめ、どの段階の失敗も ApiError に揃える。
 */
async function request<T>(path: string, init: RequestInit = {}, opts: RequestOptions = {}): Promise<T> {
  const { signal, timeoutMs } = opts;
  const controller = new AbortController();
  const abortFromCaller = () => controller.abort();
  if (signal?.aborted) controller.abort();
  else signal?.addEventListener("abort", abortFromCaller, { once: true });
  let timedOut = false;
  const timeoutId =
    timeoutMs === undefined
      ? undefined
      : window.setTimeout(() => {
          timedOut = true;
          controller.abort();
        }, timeoutMs);

  let res: Response | null = null;
  try {
    res = await fetch(`${BASE}${path}`, { ...init, signal: controller.signal });
    if (!res.ok) throw new ApiError("http", res.status, await readDetail(res));
    return (await res.json()) as T;
  } catch (error) {
    if (controller.signal.aborted) throw new ApiError(timedOut ? "timeout" : "aborted");
    if (error instanceof ApiError) throw error;
    // fetch 自体の失敗はネットワーク到達不可（バックエンド未起動など）、
    // 応答後の失敗は本文が JSON として読めなかったもの。
    throw res === null ? new ApiError("network") : new ApiError("invalid_response", res.status);
  } finally {
    window.clearTimeout(timeoutId);
    signal?.removeEventListener("abort", abortFromCaller);
  }
}

function postJson<T>(path: string, body: unknown, opts?: RequestOptions): Promise<T> {
  return request<T>(
    path,
    { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) },
    opts,
  );
}

/** バックエンド疎通確認（GET /health） */
export async function health(): Promise<boolean> {
  try {
    const data = await request<{ status?: string }>("/health");
    return data.status === "ok";
  } catch {
    return false;
  }
}

/** 推薦取得（POST /recommend） */
export function recommend(req: RecommendRequest, signal?: AbortSignal): Promise<RecommendResponse> {
  return postJson<RecommendResponse>("/recommend", req, { signal });
}

/** 履歴ベースの初期推薦（POST /recommend/home） */
export function recommendHome(req: HomeRecommendRequest, signal?: AbortSignal): Promise<RecommendResponse> {
  return postJson<RecommendResponse>("/recommend/home", req, { signal });
}

/**
 * タブを閉じる/バックグラウンドに回した時に呼ぶfire-and-forget（POST /recommend/home/warm）。
 * 次回開いた時にrecommendHome()が即座に返せるよう、バックエンド側でホーム推薦を
 * 先読みキャッシュしておく。応答は使わないので navigator.sendBeacon を優先する
 * （ページ破棄中でも送信を続けてくれるため、await可能なfetchより確実）。
 */
export function warmHomeCache(req: HomeRecommendRequest): void {
  const url = `${BASE}/recommend/home/warm`;
  const body = JSON.stringify(req);
  if (navigator.sendBeacon) {
    navigator.sendBeacon(url, new Blob([body], { type: "application/json" }));
    return;
  }
  fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body, keepalive: true }).catch(
    () => {
      /* fire-and-forgetなので失敗しても何もしない */
    },
  );
}

/** 対話型推薦の1ターン（POST /chat） */
export function chat(
  messages: ChatMessage[],
  limit = 10,
  lang: "ja" | "en" = "ja",
  userId?: string | null,
  signal?: AbortSignal,
): Promise<ChatResponse> {
  return postJson<ChatResponse>(
    "/chat",
    { messages, limit, lang, user_id: userId ?? null },
    { signal, timeoutMs: CHAT_TIMEOUT_MS },
  );
}

/** デモ用テストユーザー一覧（GET /users/sample） */
export function sampleUsers(limit = 10, signal?: AbortSignal): Promise<SampleUsersResponse> {
  return request<SampleUsersResponse>(`/users/sample?limit=${limit}`, {}, { signal });
}

/** 商品レビュー取得（GET /products/{id}/reviews） */
export function fetchReviews(
  productId: string,
  limit = 5,
  lang: "ja" | "en" = "en",
  signal?: AbortSignal,
): Promise<ReviewsResponse> {
  return request<ReviewsResponse>(
    `/products/${encodeURIComponent(productId)}/reviews?limit=${limit}&lang=${lang}`,
    {},
    { signal },
  );
}

/** 商品説明文取得（GET /products/{id}/description） */
export function fetchDescription(
  productId: string,
  lang: "ja" | "en" = "en",
  signal?: AbortSignal,
): Promise<DescriptionResponse> {
  return request<DescriptionResponse>(
    `/products/${encodeURIComponent(productId)}/description?lang=${lang}`,
    {},
    { signal },
  );
}

/** 推薦フィードバック送信（POST /recommendations/{id}/feedback） */
export function sendFeedback(
  productId: string,
  helpful: boolean,
  userId: string | null,
  searchId: string | null,
  lang: "ja" | "en" = "ja",
): Promise<FeedbackResponse> {
  return postJson<FeedbackResponse>(`/recommendations/${encodeURIComponent(productId)}/feedback`, {
    user_id: userId,
    search_id: searchId,
    helpful,
    lang,
  });
}

/** 商品閲覧ログ（POST /behavior/view）。失敗しても画面には影響させない（fire-and-forget）。 */
export async function logView(req: ViewLogRequest): Promise<void> {
  try {
    await fetch(`${BASE}/behavior/view`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(req),
    });
  } catch {
    /* 行動ログの送信失敗はユーザー操作をブロックしない */
  }
}

/**
 * このユーザーのRATED以外の永続データ（VIEWED行動ログ・SearchLog検索履歴）を削除する
 * （POST /users/{user_id}/clear_history）。RATED（データセット由来の評価履歴）は対象外。
 */
export function clearHistory(userId: string): Promise<ClearHistoryResponse> {
  return request<ClearHistoryResponse>(`/users/${encodeURIComponent(userId)}/clear_history`, {
    method: "POST",
  });
}
