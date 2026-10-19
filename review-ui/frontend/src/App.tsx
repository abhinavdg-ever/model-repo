import { useEffect, useRef, useState } from "react";
import LandingPage, { savedHistoryListParams } from "./LandingPage";
import { nextChart, type ChartSource } from "./chartSequence";
import FolderViewer from "./FolderViewer";
import FileViewer from "./FileViewer";
import AccuracyView from "./AccuracyView";
import ManualReviewView from "./ManualReviewView";
import LoginPage from "./LoginPage";
import UserProfileMenu from "./UserProfileMenu";
import { getAppConfig, type AppConfig, type OutputMode } from "./api";

const AUTH_KEY = "advantmed_imaging_auth";

type Route =
  | { view: "landing" }
  | { view: "folder"; folderId: string; mode: OutputMode; from?: ChartSource }
  | { view: "file-viewer"; folderId?: string }
  | { view: "accuracy" }
  | { view: "manual-review" };

function parseMode(raw: string | null): OutputMode {
  return raw === "imaging" ? "imaging" : "ocr";
}

function parseFrom(raw: string | null): ChartSource | undefined {
  return raw === "accuracy" || raw === "manual-review" ? raw : undefined;
}

function parsePath(pathname: string, search: string): Route {
  const params = new URLSearchParams(search);
  if (pathname === "/accuracy" || pathname === "/accuracy/") {
    return { view: "accuracy" };
  }
  if (pathname === "/manual-review" || pathname === "/manual-review/") {
    return { view: "manual-review" };
  }
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
      from: parseFrom(params.get("from")),
    };
  }
  return { view: "landing" };
}

function pathFor(route: Route): string {
  if (route.view === "folder") {
    const base = `/folders/${encodeURIComponent(route.folderId)}`;
    const params = new URLSearchParams();
    if (route.mode === "imaging") params.set("mode", "imaging");
    if (route.from) params.set("from", route.from);
    const qs = params.toString();
    return qs ? `${base}?${qs}` : base;
  }
  if (route.view === "accuracy") return "/accuracy";
  if (route.view === "manual-review") return "/manual-review";
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
  const leaveGuard = useRef<((run: () => void) => void) | null>(null);

  function guardedNavigate(next: Route) {
    const go = () => navigate(next);
    if (leaveGuard.current) leaveGuard.current(go);
    else go();
  }

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
    guardedNavigate({ view: "landing" });
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
            onClick={() => guardedNavigate({ view: "landing" })}
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
          {/* The mode is DATA_MODE in review-ui/.env; there is no switch. */}
          <span
            className={modePillClass(modeLabel)}
            title={
              modeLabel === "Production Mode"
                ? "OCR + imaging outputs read from Postgres; page images from Azure Blob"
                : "OCR + imaging outputs read from local data/folders"
            }
          >
            {modeLabel}
          </span>
          <UserProfileMenu
            displayName="imaging-user"
            initials="IU"
            currentView={route.view}
            onOpenFileViewer={() => guardedNavigate({ view: "file-viewer" })}
            onOpenHistory={() => guardedNavigate({ view: "landing" })}
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
            onOpenAccuracy={() => navigate({ view: "accuracy" })}
            onOpenManualReview={() => navigate({ view: "manual-review" })}
          />
        ) : route.view === "manual-review" ? (
          <ManualReviewView
            onBack={() => navigate({ view: "landing" })}
            onOpenChart={(folderId) =>
              navigate({ view: "folder", folderId, mode: "imaging", from: "manual-review" })
            }
            chartHref={(folderId) =>
              pathFor({ view: "folder", folderId, mode: "imaging", from: "manual-review" })
            }
          />
        ) : route.view === "accuracy" ? (
          <AccuracyView
            onBack={() => navigate({ view: "landing" })}
            onOpenChart={(folderId) =>
              navigate({ view: "folder", folderId, mode: "imaging", from: "accuracy" })
            }
            chartHref={(folderId) =>
              pathFor({ view: "folder", folderId, mode: "imaging", from: "accuracy" })
            }
          />
        ) : route.view === "file-viewer" ? (
          <FileViewer
            initialFolderId={route.folderId ?? null}
            onBack={() => navigate({ view: "landing" })}
          />
        ) : (
          <FolderViewer
            key={route.folderId}
            folderId={route.folderId}
            initialMode={route.mode}
            onBack={() =>
              navigate(route.from ? { view: route.from } : { view: "landing" })
            }
            findNextChart={() => nextChart(route.folderId, route.from, savedHistoryListParams())}
            onOpenNext={(folderId) =>
              navigate({ view: "folder", folderId, mode: route.mode, from: route.from })
            }
            onBindLeave={(guard) => {
              leaveGuard.current = guard;
            }}
            onModeChange={(mode) =>
              navigate({ view: "folder", folderId: route.folderId, mode, from: route.from })
            }
          />
        )}
      </main>
    </div>
  );
}
