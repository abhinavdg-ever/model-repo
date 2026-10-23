import type { ReactNode } from "react";
import { AlertCircle, CheckCircle2, X } from "lucide-react";

type Props = {
  kind: "success" | "error";
  children: ReactNode;
  /** Given, the message has a close button. */
  onClose?: () => void;
};

/** A page-wide success or error message: green or red, an icon, and the text. */
export default function PageMessage({ kind, children, onClose }: Props) {
  const Icon = kind === "success" ? CheckCircle2 : AlertCircle;
  return (
    <div className={`page-message page-message-${kind}`} role={kind === "error" ? "alert" : "status"}>
      <Icon size={16} aria-hidden="true" className="page-message-icon" />
      <span className="page-message-text">{children}</span>
      {onClose ? (
        <button type="button" className="page-message-close" onClick={onClose} aria-label="Dismiss">
          <X size={14} aria-hidden="true" />
        </button>
      ) : null}
    </div>
  );
}
