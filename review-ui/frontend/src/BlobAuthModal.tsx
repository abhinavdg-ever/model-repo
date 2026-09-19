import { FormEvent, useState } from "react";
import { Cloud, KeyRound, X } from "lucide-react";
import { writeSessionSas } from "./blobAuth";

type Props = {
  accountUrl: string;
  container: string;
  onCancel: () => void;
  onAuthenticated: () => void;
};

export default function BlobAuthModal({
  accountUrl,
  container,
  onCancel,
  onAuthenticated,
}: Props) {
  const [sas, setSas] = useState("");
  const [error, setError] = useState("");

  function handleSubmit(e: FormEvent) {
    e.preventDefault();
    const value = sas.trim();
    if (!value) {
      setError("Paste a SAS token to continue.");
      return;
    }
    writeSessionSas(value);
    onAuthenticated();
  }

  return (
    <div className="modal-backdrop" role="presentation" onClick={onCancel}>
      <div
        className="modal-card blob-auth-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="blob-auth-title"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal-card-header">
          <div className="blob-auth-title-row">
            <Cloud size={18} aria-hidden="true" />
            <h2 id="blob-auth-title">Blob authentication</h2>
          </div>
          <button type="button" className="modal-close" onClick={onCancel} aria-label="Close">
            <X size={16} />
          </button>
        </div>

        <p className="blob-auth-copy">
          Enter a SAS token once for this browser session to open pages from blob storage.
        </p>

        <dl className="blob-auth-meta">
          <div>
            <dt>Account</dt>
            <dd>{accountUrl || "— not configured —"}</dd>
          </div>
          <div>
            <dt>Container</dt>
            <dd>{container || "— not configured —"}</dd>
          </div>
        </dl>

        <form onSubmit={handleSubmit} className="blob-auth-form">
          <label className="login-field">
            <span>SAS token</span>
            <div className="login-input-wrap">
              <KeyRound size={16} aria-hidden="true" />
              <input
                type="password"
                name="sas"
                autoComplete="off"
                placeholder="sv=…&sig=…  (or paste full ?sv=… query)"
                value={sas}
                onChange={(e) => {
                  setSas(e.target.value);
                  setError("");
                }}
                required
              />
            </div>
          </label>

          {error ? <p className="login-error">{error}</p> : null}

          <div className="blob-auth-actions">
            <button type="button" className="ocr-footer-btn" onClick={onCancel}>
              Cancel
            </button>
            <button type="submit" className="ocr-footer-btn primary">
              Authenticate
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
