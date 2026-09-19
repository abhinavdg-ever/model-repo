import { useEffect, useRef, useState } from "react";
import { FolderOpen, History, LogOut } from "lucide-react";

type Props = {
  displayName?: string;
  initials?: string;
  /** "file-viewer" shows History link; otherwise shows File Browser link */
  currentView?: "landing" | "folder" | "file-viewer";
  onOpenFileViewer?: () => void;
  onOpenHistory?: () => void;
  onLogout: () => void;
};

export default function UserProfileMenu({
  displayName = "imaging-user",
  initials = "IU",
  currentView = "landing",
  onOpenFileViewer,
  onOpenHistory,
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
              <p>Signed in</p>
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
              File Browser
            </button>
          )}

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
