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

/** API 呼び出しの失敗を表す例外（画面側で status を出し分けられるよう保持） */
export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

/** バックエンド疎通確認（GET /health） */
export async function health(): Promise<boolean> {
  try {
    const res = await fetch(`${BASE}/health`);
    if (!res.ok) return false;
    const data = (await res.json()) as { status?: string };
    return data.status === "ok";
  } catch {
    return false;
  }
}

/** 推薦取得（POST /recommend） */
export async function recommend(
  req: RecommendRequest,
): Promise<RecommendResponse> {
  let res: Response;
  try {
    res = await fetch(`${BASE}/recommend`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(req),
    });
  } catch {
    // ネットワーク到達不可（バックエンド未起動など）
    throw new ApiError(0, "APIに接続できませんでした。バックエンドが起動しているか確認してください。");
  }

  if (!res.ok) {
    let detail = "";
    try {
      const body = (await res.json()) as { detail?: string };
      detail = body.detail ? `: ${body.detail}` : "";
    } catch {
      /* JSON でないレスポンスは無視 */
    }
    throw new ApiError(res.status, `APIエラー (${res.status})${detail}`);
  }

  return (await res.json()) as RecommendResponse;
}

/** 履歴ベースの初期推薦（POST /recommend/home） */
export async function recommendHome(
  req: HomeRecommendRequest,
): Promise<RecommendResponse> {
  let res: Response;
  try {
    res = await fetch(`${BASE}/recommend/home`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(req),
    });
  } catch {
    throw new ApiError(0, "APIに接続できませんでした。バックエンドが起動しているか確認してください。");
  }

  if (!res.ok) {
    let detail = "";
    try {
      const body = (await res.json()) as { detail?: string };
      detail = body.detail ? `: ${body.detail}` : "";
    } catch {
      /* JSON でないレスポンスは無視 */
    }
    throw new ApiError(res.status, `APIエラー (${res.status})${detail}`);
  }

  return (await res.json()) as RecommendResponse;
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
export async function chat(
  messages: ChatMessage[],
  limit = 10,
  lang: "ja" | "en" = "ja",
  userId?: string | null,
): Promise<ChatResponse> {
  let res: Response;
  try {
    res = await fetch(`${BASE}/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ messages, limit, lang, user_id: userId ?? null }),
    });
  } catch {
    throw new ApiError(0, "APIに接続できませんでした。バックエンドが起動しているか確認してください。");
  }
  if (!res.ok) {
    throw new ApiError(res.status, `APIエラー (${res.status})`);
  }
  return (await res.json()) as ChatResponse;
}

/** デモ用テストユーザー一覧（GET /users/sample） */
export async function sampleUsers(limit = 10): Promise<SampleUsersResponse> {
  const res = await fetch(`${BASE}/users/sample?limit=${limit}`);
  if (!res.ok) throw new ApiError(res.status, `APIエラー (${res.status})`);
  return (await res.json()) as SampleUsersResponse;
}

/** 商品レビュー取得（GET /products/{id}/reviews） */
export async function fetchReviews(
  productId: string,
  limit = 5,
  lang: "ja" | "en" = "en",
): Promise<ReviewsResponse> {
  const res = await fetch(`${BASE}/products/${productId}/reviews?limit=${limit}&lang=${lang}`);
  if (!res.ok) throw new ApiError(res.status, `APIエラー (${res.status})`);
  return (await res.json()) as ReviewsResponse;
}

/** 商品説明文取得（GET /products/{id}/description） */
export async function fetchDescription(
  productId: string,
  lang: "ja" | "en" = "en",
): Promise<DescriptionResponse> {
  const res = await fetch(`${BASE}/products/${productId}/description?lang=${lang}`);
  if (!res.ok) throw new ApiError(res.status, `APIエラー (${res.status})`);
  return (await res.json()) as DescriptionResponse;
}

/** 推薦フィードバック送信（POST /recommendations/{id}/feedback） */
export async function sendFeedback(
  productId: string,
  helpful: boolean,
  userId: string | null,
  searchId: string | null,
  lang: "ja" | "en" = "ja",
): Promise<FeedbackResponse> {
  const res = await fetch(`${BASE}/recommendations/${productId}/feedback`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ user_id: userId, search_id: searchId, helpful, lang }),
  });
  if (!res.ok) throw new ApiError(res.status, `APIエラー (${res.status})`);
  return (await res.json()) as FeedbackResponse;
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
export async function clearHistory(userId: string): Promise<ClearHistoryResponse> {
  const res = await fetch(`${BASE}/users/${encodeURIComponent(userId)}/clear_history`, {
    method: "POST",
  });
  if (!res.ok) throw new ApiError(res.status, `APIエラー (${res.status})`);
  return (await res.json()) as ClearHistoryResponse;
}
