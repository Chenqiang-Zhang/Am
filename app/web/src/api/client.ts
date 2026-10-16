import type {
  ChatRequest,
  ChatResponse,
  Lang,
  RecommendRequest,
  RecommendResponse,
  ReviewsResponse,
  SamplePlayersResponse,
} from "../types/recommend";

const BASE = "/api";
const CHAT_TIMEOUT_MS = 45_000;

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

async function request<T>(path: string, init?: RequestInit, timeoutMs?: number): Promise<T> {
  const controller = new AbortController();
  const timeoutId = timeoutMs ? window.setTimeout(() => controller.abort(), timeoutMs) : undefined;
  let res: Response;
  try {
    res = await fetch(`${BASE}${path}`, { ...init, signal: controller.signal });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") {
      throw new ApiError(0, "応答に時間がかかっています。しばらくしてからもう一度お試しください。");
    }
    throw new ApiError(0, "APIに接続できませんでした。バックエンドが起動しているか確認してください。");
  } finally {
    window.clearTimeout(timeoutId);
  }
  if (!res.ok) {
    throw new ApiError(res.status, `APIエラー (${res.status})`);
  }
  return (await res.json()) as T;
}

function postJson(body: unknown): RequestInit {
  return {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
}

export function chat(req: ChatRequest): Promise<ChatResponse> {
  return request("/chat", postJson(req), CHAT_TIMEOUT_MS);
}

export function recommend(req: RecommendRequest): Promise<RecommendResponse> {
  return request("/recommend", postJson(req));
}

export function samplePlayers(limit = 10): Promise<SamplePlayersResponse> {
  return request(`/players/sample?limit=${limit}`);
}

export function fetchReviews(appid: string, limit = 5): Promise<ReviewsResponse> {
  return request(`/games/${appid}/reviews?limit=${limit}`);
}
