import { useEffect, useRef, useState } from "react";
import LandingPage, { savedHistoryListParams } from "./LandingPage";
import { nextChart } from "./chartSequence";
import FolderViewer from "./FolderViewer";
import FileViewer from "./FileViewer";
import AccuracyView from "./AccuracyView";
import ManualReviewView from "./ManualReviewView";
import LoginPage from "./LoginPage";
import UserProfileMenu from "./UserProfileMenu";
import {
  readGroundTruthSource,
  writeGroundTruthSource,
  type GroundTruthSource,
} from "./groundTruthSource";
import { getAppConfig, type AppConfig, type OutputMode } from "./api";

const AUTH_KEY = "advantmed_imaging_auth";

type Route =
  | { view: "landing" }
  | { view: "folder"; folderId: string; mode: OutputMode; from?: "accuracy" }
  | { view: "file-viewer"; folderId?: string }
  | { view: "accuracy" }
  | { view: "manual-review" };

function parseMode(raw: string | null): OutputMode {
  return raw === "imaging" ? "imaging" : "ocr";
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
      from: params.get("from") === "accuracy" ? "accuracy" : undefined,
    };
  }
  return { view: "landing" };
}

function pathFor(route: Route): string {
  if (route.view === "folder") {
    const base = `/folders/${encodeURIComponent(route.folderId)}`;
    const params = new URLSearchParams();
    if (route.mode === "imaging") params.set("mode", "imaging");
    if (route.from === "accuracy") params.set("from", "accuracy");
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
  const [gtSource, setGtSource] = useState<GroundTruthSource>(readGroundTruthSource);
  const leaveGuard = useRef<((run: () => void) => void) | null>(null);

  function guardedNavigate(next: Route) {
    const go = () => navigate(next);
    if (leaveGuard.current) leaveGuard.current(go);
    else go();
  }

  // The open view remounts (key below) and refetches with the new choice, so
  // unsaved edits get the same prompt as leaving the page.
  function toggleManualGroundTruth() {
    const next: GroundTruthSource = gtSource === "bulk_upload" ? "latest" : "bulk_upload";
    const apply = () => {
      writeGroundTruthSource(next);
      setGtSource(next);
    };
    if (leaveGuard.current) leaveGuard.current(apply);
    else apply();
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
          {gtSource === "bulk_upload" ? (
            <span
              className="mode-pill gt-source-pill"
              title="Ground truth shows each page's latest bulk upload; manual edits are hidden. Change it from the user menu."
            >
              Bulk upload ground truth
            </span>
          ) : null}
          <UserProfileMenu
            displayName="imaging-user"
            initials="IU"
            currentView={route.view}
            onOpenFileViewer={() => guardedNavigate({ view: "file-viewer" })}
            onOpenHistory={() => guardedNavigate({ view: "landing" })}
            hideManualGroundTruth={gtSource === "bulk_upload"}
            onToggleManualGroundTruth={toggleManualGroundTruth}
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
            onOpenAccuracy={() => navigate({ view: "accuracy" })}
            onOpenManualReview={() => navigate({ view: "manual-review" })}
          />
        ) : route.view === "manual-review" ? (
          <ManualReviewView onBack={() => navigate({ view: "landing" })} />
        ) : route.view === "accuracy" ? (
          <AccuracyView
            key={gtSource}
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
            key={`${route.folderId}:${gtSource}`}
            folderId={route.folderId}
            initialMode={route.mode}
            onBack={() =>
              navigate(route.from === "accuracy" ? { view: "accuracy" } : { view: "landing" })
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
