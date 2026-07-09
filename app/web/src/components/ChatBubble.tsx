import { useI18n } from "../i18n";
import styles from "./ChatBubble.module.css";

interface Props {
  role: "user" | "assistant";
  content: string;
  loading?: boolean;
}

// 会話の吹き出し。assistant=左、user=右。
export default function ChatBubble({ role, content, loading }: Props) {
  const { t } = useI18n();
  const isUser = role === "user";
  return (
    <div className={isUser ? styles.userRow : styles.assistantRow}>
      <div className={isUser ? styles.userBubble : styles.assistantBubble}>
        {loading ? (
          <span className={styles.loading}>
            {t.thinking}
            <span className={styles.dots}>
              <span /><span /><span />
            </span>
          </span>
        ) : content}
      </div>
    </div>
  );
}
