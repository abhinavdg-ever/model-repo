import { useEffect, useRef, useState } from "react";
import LandingPage, { clearLandingFilters, savedHistoryListParams } from "./LandingPage";
import { nextChart, type ChartSource } from "./chartSequence";
import FolderViewer from "./FolderViewer";
import FileViewer from "./FileViewer";
import AccuracyView from "./AccuracyView";
import ManualReviewView from "./ManualReviewView";
import PlaygroundView from "./PlaygroundView";
import UsersView from "./UsersView";
import SearchView from "./SearchView";
import LoginPage from "./LoginPage";
import UserProfileMenu from "./UserProfileMenu";
import { getAppConfig, type AppConfig, type OutputMode } from "./api";
import { ClientViewContext, scoreFinalOnly, setFinalOnlyChoice, setScoreFinalOnly, setScoringAdmin } from "./clientView";
import { clearSession, initialsOf, readSession, writeSession, type Session } from "./session";


type Route =
  | { view: "landing" }
  | { view: "folder"; folderId: string; mode: OutputMode; from?: ChartSource; page?: number }
  | { view: "file-viewer"; folderId?: string }
  | { view: "accuracy" }
  | { view: "manual-review" }
  | { view: "playground" }
  | { view: "users" }
  | { view: "search" };

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
  if (pathname === "/playground" || pathname === "/playground/") {
    return { view: "playground" };
  }
  if (pathname === "/users" || pathname === "/users/") {
    return { view: "users" };
  }
  if (pathname === "/search" || pathname === "/search/") {
    return { view: "search" };
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
      page: Number(params.get("page")) > 0 ? Number(params.get("page")) : undefined,
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
    if (route.page) params.set("page", String(route.page));
    const qs = params.toString();
    return qs ? `${base}?${qs}` : base;
  }
  if (route.view === "accuracy") return "/accuracy";
  if (route.view === "manual-review") return "/manual-review";
  if (route.view === "playground") return "/playground";
  if (route.view === "users") return "/users";
  if (route.view === "search") return "/search";
  if (route.view === "file-viewer") {
    return route.folderId
      ? `/file-viewer/${encodeURIComponent(route.folderId)}`
      : "/file-viewer";
  }
  return "/";
}


function modePillClass(label: string): string {
  return label.toLowerCase().includes("production")
    ? "mode-pill mode-pill-production"
    : "mode-pill mode-pill-local";
}

export default function App() {
  const [session, setSession] = useState<Session | null>(readSession);
  const authed = session !== null;
  const isAdmin = session?.user.role === "admin";
  const [route, setRoute] = useState<Route>(() =>
    parsePath(window.location.pathname, window.location.search),
  );
  const [appConfig, setAppConfig] = useState<AppConfig | null>(null);
  // Charts are scored only once /api/config says whether this is the client's copy.
  const [configReady, setConfigReady] = useState(false);
  // Bumped when an admin switches "Accuracy on Final only": the views re-score.
  const [scoring, setScoring] = useState(0);
  setScoringAdmin(isAdmin);
  const leaveGuard = useRef<((run: () => void) => void) | null>(null);

  function guardedNavigate(next: Route) {
    const go = () => navigate(next);
    if (leaveGuard.current) leaveGuard.current(go);
    else go();
  }

  useEffect(() => {
    const onPop = () =>
      setRoute((previous) => {
        const next = parsePath(window.location.pathname, window.location.search);
        if (previous.view === "accuracy" && next.view === "landing") clearLandingFilters();
        return next;
      });
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  useEffect(() => {
    let cancelled = false;
    getAppConfig()
      .then((cfg) => {
        if (cancelled) return;
        setScoreFinalOnly(Boolean(cfg.client_view));
        setAppConfig(cfg);
        setConfigReady(true);
      })
      .catch(() => {
        if (cancelled) return;
        setAppConfig(null);
        setConfigReady(true);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  function navigate(next: Route) {
    // Accuracy View -> History: History starts unfiltered, not with filters it saved earlier.
    if (route.view === "accuracy" && next.view === "landing") clearLandingFilters();
    const path = pathFor(next);
    const current = `${window.location.pathname}${window.location.search}`;
    if (path !== current) {
      window.history.pushState(null, "", path);
    }
    setRoute(next);
  }

  function handleLoginSuccess(next: Session) {
    writeSession(next);
    setSession(next);
  }

  function handleLogout() {
    clearSession();
    setSession(null);
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
            displayName={session?.user.name || session?.user.username || "Signed in"}
            initials={session ? initialsOf(session.user) : "U"}
            role={session?.user.role}
            currentView={route.view}
            onOpenFileViewer={() => guardedNavigate({ view: "file-viewer" })}
            onOpenHistory={() => guardedNavigate({ view: "landing" })}
            onOpenPlayground={isAdmin ? () => guardedNavigate({ view: "playground" }) : undefined}
            onOpenUsers={isAdmin ? () => guardedNavigate({ view: "users" }) : undefined}
            onOpenSearch={isAdmin ? () => guardedNavigate({ view: "search" }) : undefined}
            finalOnly={scoreFinalOnly()}
            onToggleFinalOnly={
              isAdmin
                ? (on) => {
                    setFinalOnlyChoice(on);
                    setScoring((n) => n + 1);
                  }
                : undefined
            }
            onLogout={handleLogout}
          />
        </div>
      </header>
      <main className="main" key={`scoring-${scoring}`}>
        {!configReady ? (
          <div className="workspace">
            <p className="accuracy-note">Loading…</p>
          </div>
        ) : route.view === "landing" ? (
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
            onOpenChart={(folderId, page) =>
              navigate({ view: "folder", folderId, mode: "imaging", from: "manual-review", page })
            }
            chartHref={(folderId, page) =>
              pathFor({ view: "folder", folderId, mode: "imaging", from: "manual-review", page })
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
        ) : route.view === "playground" || route.view === "users" || route.view === "search" ? (
          isAdmin ? (
            route.view === "search" ? (
              <SearchView
                onBack={() => navigate({ view: "landing" })}
                chartHref={(folderId, page) => pathFor({ view: "folder", folderId, mode: "imaging", page })}
              />
            ) : route.view === "users" ? (
              <UsersView onBack={() => navigate({ view: "landing" })} />
            ) : (
              <PlaygroundView config={appConfig} onBack={() => navigate({ view: "landing" })} />
            )
          ) : (
            <div className="workspace">
              <p className="accuracy-note">This page is for admins only.</p>
            </div>
          )
        ) : route.view === "file-viewer" ? (
          <FileViewer
            initialFolderId={route.folderId ?? null}
            onBack={() => navigate({ view: "landing" })}
          />
        ) : (
          <ClientViewContext.Provider value={Boolean(appConfig?.client_view)}>
          <FolderViewer
            key={route.folderId}
            folderId={route.folderId}
            initialMode={route.mode}
            initialPage={route.page}
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
          </ClientViewContext.Provider>
        )}
      </main>
    </div>
  );
}
