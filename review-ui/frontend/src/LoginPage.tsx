import { FormEvent, useState } from "react";
import { Eye, EyeOff, Lock, User } from "lucide-react";
import { login } from "./api";
import type { Session } from "./session";

// Signs in against review-ui's users table (POST /api/auth/login). The server checks the
// password hash and records the sign-in; this screen never holds a password list.

type Props = {
  onSuccess: (session: Session) => void;
};

export default function LoginPage({ onSuccess }: Props) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setError("");
    setSubmitting(true);
    try {
      onSuccess(await login(username.trim(), password));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Sign-in failed.");
      setSubmitting(false);
    }
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
