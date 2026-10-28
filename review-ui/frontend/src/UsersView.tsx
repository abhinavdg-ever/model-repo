import { useEffect, useState } from "react";
import { formatIst } from "./istTime";
import { ArrowLeft, Eye, EyeOff, KeyRound, UserPlus } from "lucide-react";
import PageMessage from "./PageMessage";
import {
  createUser,
  errorDetail,
  listUsers,
  resetUserPassword,
  type UserRole,
  type UserRow,
} from "./api";

type Props = { onBack: () => void };
type Mode = "create" | "reset";

const MIN_PASSWORD = 8;
const ROLE_LABEL: Record<UserRole, string> = { admin: "Admin", reviewer: "Reviewer", viewer: "Viewer" };

export function roleLabel(role: string | undefined): string {
  return ROLE_LABEL[(role ?? "") as UserRole] ?? (role ? role[0].toUpperCase() + role.slice(1) : "");
}

/** A password box with an eye button that shows / hides what was typed. */
function PasswordInput({ value, onChange }: { value: string; onChange: (value: string) => void }) {
  const [shown, setShown] = useState(false);
  return (
    <div className="password-field">
      <input
        type={shown ? "text" : "password"}
        value={value}
        autoComplete="new-password"
        onChange={(e) => onChange(e.target.value)}
      />
      <button
        type="button"
        className="password-eye"
        onClick={() => setShown((v) => !v)}
        aria-label={shown ? "Hide password" : "Show password"}
        aria-pressed={shown}
        title={shown ? "Hide password" : "Show password"}
      >
        {shown ? <EyeOff size={15} aria-hidden="true" /> : <Eye size={15} aria-hidden="true" />}
      </button>
    </div>
  );
}

/** Admins only: create a user, or reset an existing user's password. */
export default function UsersView({ onBack }: Props) {
  const [mode, setMode] = useState<Mode>("create");
  const [users, setUsers] = useState<UserRow[]>([]);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [username, setUsername] = useState("");
  const [name, setName] = useState("");
  const [role, setRole] = useState<UserRole>("reviewer");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  async function load() {
    try {
      setUsers(await listUsers());
      setLoadError(null);
    } catch (err) {
      setLoadError(errorDetail(err, "Could not load the users"));
    }
  }

  useEffect(() => {
    void load();
  }, []);

  const taken = users.some((u) => u.username.toLowerCase() === username.trim().toLowerCase());
  const problem =
    !username.trim()
      ? "Give a username"
      : mode === "create" && taken
        ? `${username.trim()} already exists: reset its password instead`
        : mode === "reset" && !taken && users.length > 0
          ? `No user named ${username.trim()}: create it instead`
          : password.length < MIN_PASSWORD
            ? `The password needs at least ${MIN_PASSWORD} characters`
            : password !== confirm
              ? "The two passwords differ"
              : null;

  /** A row in the list: reset that user's password. */
  function pick(user: UserRow) {
    switchMode("reset");
    setUsername(user.username);
  }

  function switchMode(next: Mode) {
    setMode(next);
    setError(null);
    setNotice(null);
    setPassword("");
    setConfirm("");
  }

  async function submit() {
    if (problem) return;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      if (mode === "create") {
        const made = await createUser({ username: username.trim(), name: name.trim(), password, role });
        setNotice(`Created ${made.username} (${roleLabel(made.role)}).`);
        setUsername("");
        setName("");
        setRole("reviewer");
      } else {
        const changed = await resetUserPassword({ username: username.trim(), password });
        setNotice(`Password reset for ${changed.username}.`);
      }
      setPassword("");
      setConfirm("");
      await load();
    } catch (err) {
      setError(errorDetail(err, mode === "create" ? "Could not create the user" : "Could not reset the password"));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="workspace accuracy-view playground-view">
      <div className="workspace-header">
        <div className="workspace-header-start">
          <button type="button" className="back-btn" onClick={onBack}>
            <ArrowLeft size={15} aria-hidden="true" />
            Back
          </button>
          <div className="workspace-title">
            <h1>User Management</h1>
          </div>
        </div>
      </div>

      {error ? (
        <PageMessage kind="error" onClose={() => setError(null)}>
          {error}
        </PageMessage>
      ) : null}
      {notice ? (
        <PageMessage kind="success" onClose={() => setNotice(null)}>
          {notice}
        </PageMessage>
      ) : null}
      <div className="users-layout">
      <section className="accuracy-section users-form" aria-label="Create or reset">
        <div className="output-tabs" role="tablist" aria-label="User action">
          <button
            type="button"
            role="tab"
            aria-selected={mode === "create"}
            className={mode === "create" ? "active" : ""}
            onClick={() => switchMode("create")}
          >
            <UserPlus size={14} aria-hidden="true" /> Create new user
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={mode === "reset"}
            className={mode === "reset" ? "active" : ""}
            onClick={() => switchMode("reset")}
          >
            <KeyRound size={14} aria-hidden="true" /> Reset password
          </button>
        </div>
        <p className="accuracy-note">
          {mode === "create"
            ? "A new sign-in. The username must not exist yet (letters are compared without case)."
            : "A new password for an existing user. Their name and role stay as they are."}
        </p>

        <div className="playground-form">
          <label>
            <span>Username</span>
            {mode === "reset" && users.length ? (
              <select value={username} onChange={(e) => setUsername(e.target.value)}>
                <option value="">Choose a user…</option>
                {users.map((u) => (
                  <option key={u.username} value={u.username}>
                    {u.username} — {u.name} ({roleLabel(u.role)})
                  </option>
                ))}
              </select>
            ) : (
              <input value={username} autoComplete="off" onChange={(e) => setUsername(e.target.value)} />
            )}
          </label>
          {mode === "create" ? (
            <>
              <label>
                <span>Name (optional)</span>
                <input value={name} placeholder="Shown in the profile" onChange={(e) => setName(e.target.value)} />
              </label>
              <label>
                <span>Role</span>
                <select value={role} onChange={(e) => setRole(e.target.value as UserRole)}>
                  <option value="reviewer">Reviewer</option>
                  <option value="admin">Admin</option>
                  <option value="viewer">Viewer</option>
                </select>
              </label>
            </>
          ) : null}
          <label>
            <span>{mode === "create" ? "Password" : "New password"}</span>
            <PasswordInput value={password} onChange={setPassword} />
          </label>
          <label>
            <span>Confirm password</span>
            <PasswordInput value={confirm} onChange={setConfirm} />
          </label>
          {problem && (username || password || confirm) ? <small className="playground-error">{problem}</small> : null}
          <div>
            <button
              type="button"
              className="landing-file-viewer-btn"
              disabled={Boolean(problem) || busy}
              onClick={submit}
            >
              {mode === "create" ? <UserPlus size={14} aria-hidden="true" /> : <KeyRound size={14} aria-hidden="true" />}
              {busy ? "Saving…" : mode === "create" ? "Create user" : "Reset password"}
            </button>
          </div>
        </div>
      </section>

      <section className="accuracy-section users-list" aria-label="Users">
        <h2>Users ({users.length})</h2>
        <p className="accuracy-note">Click a user to reset their password.</p>
        {loadError ? <PageMessage kind="error">{loadError}</PageMessage> : null}
        <table className="playground-users">
          <thead>
            <tr>
              <th>Username</th>
              <th>Name</th>
              <th>Role</th>
              <th>Active</th>
              <th>Last sign-in</th>
            </tr>
          </thead>
          <tbody>
            {users.map((u) => (
              <tr
                key={u.username}
                className={mode === "reset" && u.username === username ? "selected" : ""}
                onClick={() => pick(u)}
                tabIndex={0}
                onKeyDown={(e) => {
                  if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault();
                    pick(u);
                  }
                }}
                title={`Reset the password of ${u.username}`}
              >
                <td>{u.username}</td>
                <td>{u.name}</td>
                <td>{roleLabel(u.role)}</td>
                <td>{u.isActive ? "Yes" : "No"}</td>
                <td>{formatIst(u.lastLoginAt, "Never")}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
      </div>
    </div>
  );
}
