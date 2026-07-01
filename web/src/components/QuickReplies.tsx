import { useState } from "react";
import { useI18n } from "../i18n";
import styles from "./QuickReplies.module.css";

interface Props {
  options: string[];
  onPick: (option: string) => void;
  allowOther?: boolean;
  disabled?: boolean;
}

// 聞き返しへのワンタップ回答。allowOther で「その他（自由に入力）」を表示し、
// 押すとその場で入力欄に展開して自由入力 → 送信できる。
export default function QuickReplies({ options, onPick, allowOther, disabled }: Props) {
  const { t } = useI18n();
  const [othering, setOthering] = useState(false);
  const [text, setText] = useState("");

  if (options.length === 0) return null;

  function submitOther(e: React.FormEvent) {
    e.preventDefault();
    const value = text.trim();
    if (!value) return;
    onPick(value);
    setText("");
    setOthering(false);
  }

  return (
    <div className={styles.wrap}>
      {options.map((o) => (
        <button
          key={o}
          type="button"
          className={styles.chip}
          onClick={() => onPick(o)}
          disabled={disabled}
        >
          {o}
        </button>
      ))}

      {allowOther && !othering && (
        <button
          type="button"
          className={styles.other}
          onClick={() => setOthering(true)}
          disabled={disabled}
        >
          {t.other}
        </button>
      )}

      {allowOther && othering && (
        <form className={styles.otherForm} onSubmit={submitOther}>
          <input
            className={styles.otherInput}
            type="text"
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder={t.otherPlaceholder}
            aria-label={t.other}
            autoFocus
            disabled={disabled}
          />
          <button
            type="submit"
            className={styles.otherSend}
            disabled={disabled || !text.trim()}
          >
            {t.send}
          </button>
        </form>
      )}
    </div>
  );
}
