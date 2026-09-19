import { FormEvent, useState } from "react";
import { Eye, EyeOff, Lock, User } from "lucide-react";

// Shared POC gate, NOT authentication. Vite inlines these into the JS bundle
// at build time, so anyone who loads the page can read them — and they are in
// this repository for the same reason, deliberately: the pair is shared with
// everyone who uses the POC and protects nothing. It keeps a casual visitor
// off the page; it does not keep anyone out.
//
// The backend has no auth on any route, so the data behind this screen is
// reachable without ever seeing it. Real auth is still to be built — see the
// "no authentication" note in README.md and docs/ARCHITECTURE.md § Known
// limits. Before any deployment carrying real member data, this pair stops
// being adequate and the gap is the backend, not this file.
//
// Override per deployment at BUILD time (Vite inlines at build, so a rebuild
// is required — `docker compose up -d --build`):
//   VITE_LOGIN_USERNAME / VITE_LOGIN_PASSWORD
const VALID_USERNAME = import.meta.env.VITE_LOGIN_USERNAME ?? "imaging-user";
const VALID_PASSWORD = import.meta.env.VITE_LOGIN_PASSWORD ?? "aipocpw2026";

type Props = {
  onSuccess: () => void;
};

export default function LoginPage({ onSuccess }: Props) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);

  function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setError("");
    setSubmitting(true);

    const ok =
      username.trim() === VALID_USERNAME && password === VALID_PASSWORD;

    window.setTimeout(() => {
      if (!ok) {
        setError("Invalid username or password.");
        setSubmitting(false);
        return;
      }
      onSuccess();
    }, 180);
  }

  return (
    <div className="login-page">
      <div className="login-brand">
        <img
          src="/advantmed-wordmark.svg"
          alt="Advantmed"
          className="login-wordmark"
        />
      </div>

      <form className="login-card" onSubmit={handleSubmit} noValidate>
        <h1>Document Processing AI Platform</h1>
        <p className="login-subtitle">
          Access OCR and end-to-end Imaging Pipeline Results.
        </p>

        <label className="login-field">
          <span>Username</span>
          <div className="login-input-wrap">
            <User size={16} aria-hidden="true" />
            <input
              type="text"
              name="username"
              autoComplete="username"
              placeholder="Enter your username"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              required
            />
          </div>
        </label>

        <label className="login-field">
          <span>Password</span>
          <div className="login-input-wrap">
            <Lock size={16} aria-hidden="true" />
            <input
              type={showPassword ? "text" : "password"}
              name="password"
              autoComplete="current-password"
              placeholder="Enter your password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
            />
            <button
              type="button"
              className="login-eye"
              onClick={() => setShowPassword((v) => !v)}
              aria-label={showPassword ? "Hide password" : "Show password"}
            >
              {showPassword ? <EyeOff size={16} /> : <Eye size={16} />}
            </button>
          </div>
        </label>

        {error ? <p className="login-error">{error}</p> : null}

        <button type="submit" className="login-submit" disabled={submitting}>
          {submitting ? "Signing in…" : "Sign In"}
        </button>
      </form>

      <footer className="login-footer">
        <p>© 2026 Advantmed. All rights reserved.</p>
      </footer>
    </div>
  );
}
