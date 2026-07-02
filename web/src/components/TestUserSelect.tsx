import { useEffect, useState } from "react";
import { sampleUsers } from "../api/client";
import type { SampleUser } from "../types/recommend";
import styles from "./TestUserSelect.module.css";

// デモ用: テスト用ユーザーID を選択する。個人化(過去の評価・閲覧履歴)の効果を確認するため。
// - ORIGINAL: 履歴を持たない固定のテストユーザー（コールドスタートの動作確認用）
// - 実ユーザー: /users/sample から取得した、評価履歴を持つ実データのユーザー
const ORIGINAL_TEST_USER_ID = "demo-original-test-user";
const STORAGE_KEY = "kg_demo_user_id";

interface Props {
  userId: string | null;
  onChange: (userId: string | null) => void;
}

export function useStoredTestUserId(): [string | null, (id: string | null) => void] {
  const [userId, setUserId] = useState<string | null>(() => {
    try {
      return localStorage.getItem(STORAGE_KEY);
    } catch {
      return null;
    }
  });
  const update = (id: string | null) => {
    setUserId(id);
    try {
      if (id) localStorage.setItem(STORAGE_KEY, id);
      else localStorage.removeItem(STORAGE_KEY);
    } catch {
      /* localStorage 不可の環境では保存をスキップ */
    }
  };
  return [userId, update];
}

export default function TestUserSelect({ userId, onChange }: Props) {
  const [realUsers, setRealUsers] = useState<SampleUser[]>([]);

  useEffect(() => {
    sampleUsers(10).then((res) => setRealUsers(res.users)).catch(() => setRealUsers([]));
  }, []);

  return (
    <select
      className={styles.select}
      value={userId ?? ""}
      onChange={(e) => onChange(e.target.value || null)}
      aria-label="テストユーザー"
    >
      <option value="">匿名（個人化なし）</option>
      <option value={ORIGINAL_TEST_USER_ID}>オリジナルテストユーザー（履歴なし）</option>
      {realUsers.map((u) => (
        <option key={u.user_id} value={u.user_id}>
          実ユーザー: {u.user_id}（評価{u.rated_count}件）
        </option>
      ))}
    </select>
  );
}
