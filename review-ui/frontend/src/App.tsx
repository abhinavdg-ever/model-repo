import { useEffect, useState } from "react";
import LandingPage from "./LandingPage";
import FolderViewer from "./FolderViewer";
import FileViewer from "./FileViewer";
import LoginPage from "./LoginPage";
import UserProfileMenu from "./UserProfileMenu";
import { getAppConfig, type AppConfig, type OutputMode } from "./api";

const AUTH_KEY = "advantmed_imaging_auth";

type Route =
  | { view: "landing" }
  | { view: "folder"; folderId: string; mode: OutputMode }
  | { view: "file-viewer"; folderId?: string };

function parseMode(raw: string | null): OutputMode {
  return raw === "imaging" ? "imaging" : "ocr";
}

function parsePath(pathname: string, search: string): Route {
  const params = new URLSearchParams(search);
  const fileMatch = pathname.match(/^\/file-viewer(?:\/([^/]+))?\/?$/);
  if (fileMatch) {
    return {
      view: "file-viewer",
      folderId: fileMatch[1] ? decodeURIComponent(fileMatch[1]) : undefined,
    };
  }
  const match = pathname.match(/^\/folders\/([^/]+)\/?$/);
  if (match) {
    return {
      view: "folder",
      folderId: decodeURIComponent(match[1]),
      mode: parseMode(params.get("mode")),
    };
  }
  return { view: "landing" };
}

function pathFor(route: Route): string {
  if (route.view === "folder") {
    const base = `/folders/${encodeURIComponent(route.folderId)}`;
    return route.mode === "imaging" ? `${base}?mode=imaging` : base;
  }
  if (route.view === "file-viewer") {
    return route.folderId
      ? `/file-viewer/${encodeURIComponent(route.folderId)}`
      : "/file-viewer";
  }
  return "/";
}

function readAuth(): boolean {
  try {
    return sessionStorage.getItem(AUTH_KEY) === "1";
  } catch {
    return false;
  }
}

function modePillClass(label: string): string {
  return label.toLowerCase().includes("production")
    ? "mode-pill mode-pill-production"
    : "mode-pill mode-pill-local";
}

export default function App() {
  const [authed, setAuthed] = useState(readAuth);
  const [route, setRoute] = useState<Route>(() =>
    parsePath(window.location.pathname, window.location.search),
  );
  const [appConfig, setAppConfig] = useState<AppConfig | null>(null);

  useEffect(() => {
    const onPop = () => setRoute(parsePath(window.location.pathname, window.location.search));
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  useEffect(() => {
    let cancelled = false;
    getAppConfig()
      .then((cfg) => {
        if (!cancelled) setAppConfig(cfg);
      })
      .catch(() => {
        if (!cancelled) setAppConfig(null);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  function navigate(next: Route) {
    const path = pathFor(next);
    const current = `${window.location.pathname}${window.location.search}`;
    if (path !== current) {
      window.history.pushState(null, "", path);
    }
    setRoute(next);
  }

  function handleLoginSuccess() {
    try {
      sessionStorage.setItem(AUTH_KEY, "1");
    } catch {
      /* ignore */
    }
    setAuthed(true);
  }

  function handleLogout() {
    try {
      sessionStorage.removeItem(AUTH_KEY);
    } catch {
      /* ignore */
    }
    setAuthed(false);
    navigate({ view: "landing" });
  }

  const modeLabel =
    appConfig?.mode_label ||
    (appConfig?.data_mode === "production" || appConfig?.data_mode === "postgres"
      ? "Production Mode"
      : "Local Mode");

  if (!authed) {
    return (
      <div className="shell shell-login">
        <LoginPage onSuccess={handleLoginSuccess} />
      </div>
    );
  }

  return (
    <div className="shell">
      <header className="topbar">
        <div className="brand">
          <button
            type="button"
            className="brand-home"
            onClick={() => navigate({ view: "landing" })}
            aria-label="Go to home"
          >
            <img
              className="brand-wordmark"
              src="/advantmed-wordmark.svg"
              alt="Advantmed"
            />
          </button>
          <div>
            <strong>Document Processing AI</strong>
            <p>End to End Imaging Pipeline Results</p>
          </div>
        </div>
        <div className="topbar-meta">
          <span
            className={modePillClass(modeLabel)}
            title={
              modeLabel === "Production Mode"
                ? "OCR + imaging outputs read from Postgres"
                : "OCR + imaging outputs read from local data/folders"
            }
          >
            {modeLabel}
          </span>
          <UserProfileMenu
            displayName="imaging-user"
            initials="IU"
            currentView={route.view}
            onOpenFileViewer={() => navigate({ view: "file-viewer" })}
            onOpenHistory={() => navigate({ view: "landing" })}
            onLogout={handleLogout}
          />
        </div>
      </header>
      <main className="main">
        {route.view === "landing" ? (
          <LandingPage
            onView={(folderId, mode = "ocr") =>
              navigate({ view: "folder", folderId, mode })
            }
            onOpenFileViewer={() => navigate({ view: "file-viewer" })}
          />
        ) : route.view === "file-viewer" ? (
          <FileViewer
            initialFolderId={route.folderId ?? null}
            onBack={() => navigate({ view: "landing" })}
          />
        ) : (
          <FolderViewer
            folderId={route.folderId}
            initialMode={route.mode}
            onBack={() => navigate({ view: "landing" })}
            onModeChange={(mode) =>
              navigate({ view: "folder", folderId: route.folderId, mode })
            }
          />
        )}
      </main>
    </div>
  );
}
