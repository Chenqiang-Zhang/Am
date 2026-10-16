import { useEffect, useState } from "react";
import { samplePlayers } from "../api/client";
import type { SamplePlayer } from "../types/recommend";
import { useI18n } from "../i18n";
import styles from "./TestUserSelect.module.css";

const STORAGE_KEY = "kg_demo_player_id";

interface Props {
  playerId: string;
  onChange: (playerId: string) => void;
}

export function useStoredPlayerId(): [string, (id: string) => void] {
  const [playerId, setPlayerId] = useState<string>(() => {
    try {
      return localStorage.getItem(STORAGE_KEY) || "";
    } catch {
      return "";
    }
  });
  const update = (id: string) => {
    setPlayerId(id);
    try {
      localStorage.setItem(STORAGE_KEY, id);
    } catch {
      return;
    }
  };
  return [playerId, update];
}

export default function TestUserSelect({ playerId, onChange }: Props) {
  const { t } = useI18n();
  const [players, setPlayers] = useState<SamplePlayer[]>([]);

  useEffect(() => {
    samplePlayers(10).then((res) => setPlayers(res.players)).catch(() => setPlayers([]));
  }, []);

  return (
    <select
      className={styles.select}
      value={playerId}
      onChange={(e) => onChange(e.target.value)}
      aria-label="player"
    >
      <option value="">{t.anonymous}</option>
      {players.map((p, i) => (
        <option key={p.player_id} value={p.player_id}>
          {t.player(i + 1, p.played_count)}
        </option>
      ))}
    </select>
  );
}
