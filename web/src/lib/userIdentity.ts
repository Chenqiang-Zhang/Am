import { useEffect, useState } from "react";

const STORAGE_KEY = "am_user_id";

function createAnonId(): string {
  const random = Math.random().toString(36).slice(2, 10);
  return `anon_${random}`;
}

function readUserId(): string {
  const existing = window.localStorage.getItem(STORAGE_KEY);
  if (existing) return existing;
  const created = createAnonId();
  window.localStorage.setItem(STORAGE_KEY, created);
  return created;
}

export function useUserIdentity() {
  const [userId, setUserIdState] = useState(() => readUserId());

  useEffect(() => {
    window.localStorage.setItem(STORAGE_KEY, userId);
  }, [userId]);

  function setUserId(next: string) {
    const cleaned = next.trim().replace(/[^A-Za-z0-9_.:-]+/g, "_").slice(0, 80);
    if (cleaned) setUserIdState(cleaned);
  }

  function resetUserId() {
    setUserIdState(createAnonId());
  }

  return { userId, setUserId, resetUserId };
}
