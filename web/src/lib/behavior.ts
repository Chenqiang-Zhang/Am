import { sendBehaviorEvent } from "../api/client";

interface BehaviorPayload {
  userId: string;
  eventType: string;
  productId?: string | null;
  productIds?: string[];
  query?: string | null;
  rank?: number | null;
  source?: string;
  metadata?: Record<string, string | number | boolean | null>;
}

export function trackBehavior(payload: BehaviorPayload) {
  if (!payload.userId) return;
  void sendBehaviorEvent({
    user_id: payload.userId,
    event_type: payload.eventType,
    product_id: payload.productId,
    product_ids: payload.productIds,
    query: payload.query,
    rank: payload.rank,
    source: payload.source ?? "chat",
    metadata: payload.metadata ?? {},
  }).catch(() => {
    // Behavior logging should never block the shopping flow.
  });
}
