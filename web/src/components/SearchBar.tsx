import { useState } from "react";
import styles from "./SearchBar.module.css";

// デモの再現性のため、代表的な例クエリを用意しておく。
const EXAMPLE_QUERIES = [
  "I have dry sensitive skin, looking for a gentle fragrance-free moisturizer with hyaluronic acid",
  "vitamin C brightening serum for oily skin",
  "lightweight daily sunscreen, no white cast",
  "nourishing repair shampoo for damaged hair",
];

const LIMIT_OPTIONS = [5, 10, 20];

interface Props {
  onSearch: (query: string, limit: number) => void;
  loading: boolean;
}

export default function SearchBar({ onSearch, loading }: Props) {
  const [query, setQuery] = useState("");
  const [limit, setLimit] = useState(10);

  function submit(e: React.FormEvent) {
    e.preventDefault();
    const trimmed = query.trim();
    if (!trimmed || loading) return;
    onSearch(trimmed, limit);
  }

  function useExample(example: string) {
    setQuery(example);
    onSearch(example, limit);
  }

  return (
    <div className={styles.wrapper}>
      <form className={styles.form} onSubmit={submit}>
        <input
          className={styles.input}
          type="text"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="探している商品を自然な言葉で入力（例：乾燥肌向けの低刺激な保湿クリーム）"
          aria-label="検索クエリ"
        />
        <select
          className={styles.select}
          value={limit}
          onChange={(e) => setLimit(Number(e.target.value))}
          aria-label="表示件数"
        >
          {LIMIT_OPTIONS.map((n) => (
            <option key={n} value={n}>
              {n}件
            </option>
          ))}
        </select>
        <button
          className={styles.button}
          type="submit"
          disabled={loading || !query.trim()}
        >
          {loading ? "検索中…" : "検索"}
        </button>
      </form>

      <div className={styles.examples}>
        <span className={styles.examplesLabel}>例クエリ:</span>
        {EXAMPLE_QUERIES.map((ex) => (
          <button
            key={ex}
            type="button"
            className={styles.exampleChip}
            onClick={() => useExample(ex)}
            disabled={loading}
            title={ex}
          >
            {ex.length > 38 ? ex.slice(0, 38) + "…" : ex}
          </button>
        ))}
      </div>
    </div>
  );
}
