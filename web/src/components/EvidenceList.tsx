import styles from "./EvidenceList.module.css";

interface Props {
  evidence: string[];
}

// 商品メタデータの特徴文の引用（推薦の根拠テキスト）。
export default function EvidenceList({ evidence }: Props) {
  if (evidence.length === 0) return null;
  return (
    <ul className={styles.list}>
      {evidence.map((e, i) => (
        <li key={i} className={styles.item}>
          “{e}”
        </li>
      ))}
    </ul>
  );
}
