import { useEffect, useRef, useState } from "react";
import { FlaskConical, FolderOpen, History, LogOut, Search, Target, Users } from "lucide-react";
import { roleLabel } from "./UsersView";

type Props = {
  displayName?: string;
  initials?: string;
  /** The signed-in user's role, shown under the name (Admin / Reviewer / Viewer). */
  role?: string;
  /** "file-viewer" shows a History link; otherwise a File Viewer link */
  currentView?: "landing" | "folder" | "file-viewer" | "accuracy" | "manual-review" | "playground" | "users" | "search";
  onOpenFileViewer?: () => void;
  /** Admins only: given, the menu offers the Playground. */
  onOpenPlayground?: () => void;
  /** Admins only: given, the menu offers Users (create a user / reset a password). */
  onOpenUsers?: () => void;
  /** Admins only: given, the menu offers Search (a word in the Tesseract OCR). */
  onOpenSearch?: () => void;
  onOpenHistory?: () => void;
  /** Admins only: given, the menu offers "Accuracy on Final only: Yes / No". */
  finalOnly?: boolean;
  onToggleFinalOnly?: (on: boolean) => void;
  onLogout: () => void;
};

export default function UserProfileMenu({
  displayName = "imaging-user",
  initials = "IU",
  role,
  currentView = "landing",
  onOpenFileViewer,
  onOpenHistory,
  onOpenPlayground,
  onOpenUsers,
  onOpenSearch,
  finalOnly = false,
  onToggleFinalOnly,
  onLogout,
}: Props) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const onFileViewer = currentView === "file-viewer";

  useEffect(() => {
    if (!open) return;
    function onDocClick(e: MouseEvent) {
      if (!rootRef.current?.contains(e.target as Node)) setOpen(false);
    }
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") setOpen(false);
    }
    const timer = window.setTimeout(() => {
      document.addEventListener("mousedown", onDocClick);
    }, 0);
    document.addEventListener("keydown", onKey);
    return () => {
      window.clearTimeout(timer);
      document.removeEventListener("mousedown", onDocClick);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  return (
    <div className="user-menu" ref={rootRef}>
      <button
        type="button"
        className={`user-avatar-btn${open ? " open" : ""}`}
        onClick={() => setOpen((v) => !v)}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label="User profile"
        title={displayName}
      >
        <span className="user-avatar">{initials}</span>
      </button>
      {open ? (
        <div className="user-menu-dropdown" role="menu">
          <div className="user-menu-header">
            <div>
              <strong>{displayName}</strong>
              <p>Signed in{role ? ` · ${roleLabel(role)}` : ""}</p>
            </div>
          </div>

          {onFileViewer ? (
            <button
              type="button"
              className="user-menu-item"
              role="menuitem"
              onClick={() => {
                setOpen(false);
                onOpenHistory?.();
              }}
            >
              <History size={14} aria-hidden="true" />
              History
            </button>
          ) : (
            <button
              type="button"
              className="user-menu-item"
              role="menuitem"
              onClick={() => {
                setOpen(false);
                onOpenFileViewer?.();
              }}
            >
              <FolderOpen size={14} aria-hidden="true" />
              File Viewer
            </button>
          )}

          {onToggleFinalOnly ? (
            <button
              type="button"
              className="user-menu-item user-menu-switch-row"
              role="switch"
              aria-checked={finalOnly}
              title="On: Accuracy and the page marks score the Final value alone. Off: the better of Final and Extracted."
              onClick={() => onToggleFinalOnly(!finalOnly)}
            >
              <Target size={14} aria-hidden="true" />
              <span className="user-menu-switch-label">Accuracy on Final only</span>
              <span className={`user-menu-switch${finalOnly ? " is-on" : ""}`} aria-hidden="true">
                <span className="user-menu-switch-knob" />
              </span>
            </button>
          ) : null}

          {onOpenPlayground && currentView !== "playground" ? (
            <button
              type="button"
              className="user-menu-item"
              role="menuitem"
              onClick={() => {
                setOpen(false);
                onOpenPlayground();
              }}
            >
              <FlaskConical size={14} aria-hidden="true" />
              Playground
            </button>
          ) : null}

          {onOpenSearch && currentView !== "search" ? (
            <button
              type="button"
              className="user-menu-item"
              role="menuitem"
              onClick={() => {
                setOpen(false);
                onOpenSearch();
              }}
            >
              <Search size={14} aria-hidden="true" />
              Search
            </button>
          ) : null}

          {onOpenUsers && currentView !== "users" ? (
            <button
              type="button"
              className="user-menu-item"
              role="menuitem"
              onClick={() => {
                setOpen(false);
                onOpenUsers();
              }}
            >
              <Users size={14} aria-hidden="true" />
              User Management
            </button>
          ) : null}

          <button
            type="button"
            className="user-menu-item"
            role="menuitem"
            onClick={() => {
              setOpen(false);
              onLogout();
            }}
          >
            <LogOut size={14} aria-hidden="true" />
            Log out
          </button>
        </div>
      ) : null}
    </div>
  );
}
