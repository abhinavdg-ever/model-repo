import { useEffect, useMemo, useRef, useState } from "react";
import {
  ArrowLeft,
  ChevronLeft,
  ChevronRight,
  Cloud,
  FolderOpen,
  HardDrive,
  KeyRound,
  Maximize2,
  Minimize2,
  Search,
  ZoomIn,
  ZoomOut,
} from "lucide-react";
import {
  getAppConfig,
  getFolder,
  listFolders,
  blobPageImageUrl,
  pageImageUrl,
  type AppConfig,
  type FolderDetail,
  type FolderSummary,
} from "./api";
import {
  buildBlobObjectUrl,
  clearSessionSas,
  isBlobReady,
  readSessionSas,
} from "./blobAuth";
import BlobAuthModal from "./BlobAuthModal";
import FullscreenPageChrome from "./FullscreenPageChrome";
import PageJump from "./PageJump";
import { useImagePan } from "./useImagePan";
import { usePageViewerHotkeys } from "./usePageViewerHotkeys";

type Props = {
  onBack: () => void;
  initialFolderId?: string | null;
};

type ViewerSource = "local" | "blob";

const ZOOM_MIN = 0.5;
const ZOOM_MAX = 3;
const ZOOM_STEP = 0.25;

export default function FileViewer({ onBack, initialFolderId = null }: Props) {
  const [folders, setFolders] = useState<FolderSummary[]>([]);
  const [query, setQuery] = useState("");
  const [selectedId, setSelectedId] = useState<string | null>(initialFolderId);
  const [detail, setDetail] = useState<FolderDetail | null>(null);
  const [pageIndex, setPageIndex] = useState(0);
  const [loadingList, setLoadingList] = useState(true);
  const [loadingDetail, setLoadingDetail] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [source, setSource] = useState<ViewerSource>("local");
  const [config, setConfig] = useState<AppConfig | null>(null);
  const [showBlobAuth, setShowBlobAuth] = useState(false);
  const [blobReady, setBlobReady] = useState(false);
  const [zoom, setZoom] = useState(1);
  const [isFullscreen, setIsFullscreen] = useState(false);
  const pageStageRef = useRef<HTMLDivElement>(null);
  const {
    resetPan,
    imageStyle,
    stageProps,
    stageClassName,
  } = useImagePan(zoom, `${selectedId ?? ""}:${pageIndex}:${source}`);

  function resetZoom() {
    setZoom(1);
    resetPan();
  }

  useEffect(() => {
    let cancelled = false;
    getAppConfig()
      .then((cfg) => {
        if (cancelled) return;
        setConfig(cfg);
        setBlobReady(isBlobReady(cfg));
      })
      .catch(() => {
        if (!cancelled) {
          setConfig({
            data_mode: "local",
            file_viewer_blob_enabled: false,
            blob_auth_mode: "entra",
            blob_account_url: "",
            blob_container: "",
            blob_path_template: "{folder}/pages/{filename}",
            blob_entra_ready: false,
            blob_auth_required: false,
            blob_sas_configured: false,
          });
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    setLoadingList(true);
    listFolders()
      .then((data) => {
        if (!cancelled) setFolders(data);
      })
      .catch((err) => {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : "Failed to load folders");
        }
      })
      .finally(() => {
        if (!cancelled) setLoadingList(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!selectedId) {
      setDetail(null);
      setPageIndex(0);
      return;
    }
    let cancelled = false;
    setLoadingDetail(true);
    setError(null);
    setPageIndex(0);
    setZoom(1);
    getFolder(selectedId)
      .then((data) => {
        if (!cancelled) setDetail(data);
      })
      .catch((err) => {
        if (!cancelled) {
          setDetail(null);
          setError(err instanceof Error ? err.message : "Failed to load folder");
        }
      })
      .finally(() => {
        if (!cancelled) setLoadingDetail(false);
      });
    return () => {
      cancelled = true;
    };
  }, [selectedId]);

  useEffect(() => {
    function onFsChange() {
      setIsFullscreen(document.fullscreenElement === pageStageRef.current);
    }
    document.addEventListener("fullscreenchange", onFsChange);
    return () => document.removeEventListener("fullscreenchange", onFsChange);
  }, []);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return folders;
    return folders.filter((f) => f.name.toLowerCase().includes(q));
  }, [folders, query]);

  const blobEnabled = Boolean(config?.file_viewer_blob_enabled);
  const pageCount = detail?.pages.length ?? 0;
  const page = detail?.pages[pageIndex] ?? null;

  function requestBlobMode() {
    if (!config?.file_viewer_blob_enabled) return;
    if (isBlobReady(config)) {
      setBlobReady(true);
      setSource("blob");
      return;
    }
    // Legacy SAS mode only — Entra is server-side and needs .env filled in
    if (config.blob_auth_mode === "sas") {
      setShowBlobAuth(true);
      return;
    }
    setError(
      "Blob (Entra) is not ready. Set BLOB_ACCOUNT_URL, BLOB_CONTAINER, and AZURE_* credentials (or Managed Identity) in .env.",
    );
  }

  function imageSrc(pageNumber: number, filename: string): string {
    if (!selectedId) return "";
    if (source !== "blob" || !config) {
      return pageImageUrl(selectedId, pageNumber);
    }
    // Entra and server SAS both go through the API proxy/redirect
    if (config.blob_auth_mode === "entra" || config.blob_sas_configured) {
      return blobPageImageUrl(selectedId, pageNumber);
    }
    const sas = readSessionSas();
    return buildBlobObjectUrl(config, selectedId, filename, pageNumber, sas);
  }

  function zoomBy(delta: number) {
    setZoom((z) => Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, Math.round((z + delta) * 100) / 100)));
  }

  async function toggleFullscreen() {
    const el = pageStageRef.current;
    if (!el) return;
    try {
      if (document.fullscreenElement === el) {
        await document.exitFullscreen();
      } else {
        await el.requestFullscreen();
      }
    } catch {
      /* ignore */
    }
  }

  function exitFullscreen() {
    if (document.fullscreenElement) {
      void document.exitFullscreen();
    }
  }

  usePageViewerHotkeys({
    enabled: Boolean(detail && pageCount > 0 && !(source === "blob" && !blobReady)),
    pageCount,
    setPageIndex,
    zoomBy,
    setZoom,
    zoomStep: ZOOM_STEP,
    isFullscreen,
    onExitFullscreen: exitFullscreen,
  });

  return (
    <div className="workspace file-viewer">
      <div className="workspace-header">
        <div className="workspace-header-start">
          <button type="button" className="back-btn" onClick={onBack}>
            <ArrowLeft size={15} aria-hidden="true" />
            Back
          </button>
          <div className="workspace-title">
            <h1>File Viewer</h1>
            <p>
              Browse folders and toggle through pages
              {detail ? ` · ${detail.name}` : ""}
            </p>
          </div>
        </div>

        <div className="viewer-source-toggle" role="group" aria-label="Viewer source">
          <button
            type="button"
            className={`viewer-source-btn${source === "local" ? " active" : ""}`}
            onClick={() => setSource("local")}
            aria-pressed={source === "local"}
          >
            <HardDrive size={14} aria-hidden="true" />
            Local
          </button>
          <button
            type="button"
            className={`viewer-source-btn${source === "blob" ? " active" : ""}`}
            onClick={requestBlobMode}
            disabled={!blobEnabled}
            title={
              blobEnabled
                ? config?.blob_auth_mode === "entra"
                  ? config.blob_entra_ready
                    ? "Blob via Microsoft Entra ID"
                    : "Configure BLOB_ACCOUNT_URL / BLOB_CONTAINER / AZURE_* in .env"
                  : blobReady
                    ? "Blob viewer"
                    : "Authenticate once to use Blob"
                : "Enable FILE_VIEWER_BLOB_ENABLED in .env"
            }
            aria-pressed={source === "blob"}
          >
            <Cloud size={14} aria-hidden="true" />
            Blob
            {blobEnabled && config?.blob_auth_required && !blobReady ? (
              <KeyRound size={12} aria-hidden="true" />
            ) : null}
          </button>
          {source === "blob" && blobReady && config?.blob_auth_required ? (
            <button
              type="button"
              className="viewer-source-btn ghost"
              onClick={() => {
                clearSessionSas();
                setBlobReady(false);
                setSource("local");
              }}
              title="Clear session blob auth"
            >
              Clear auth
            </button>
          ) : null}
        </div>
      </div>

      {error && <div className="error-banner">{error}</div>}

      <div className="file-viewer-split">
        <aside className="file-viewer-sidebar" aria-label="Folders">
          <label className="file-viewer-search">
            <Search size={14} aria-hidden="true" />
            <input
              type="search"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search folders…"
              aria-label="Search folders"
            />
          </label>
          <div className="file-viewer-folder-list">
            {loadingList ? (
              <p className="file-viewer-muted">Loading folders…</p>
            ) : filtered.length === 0 ? (
              <p className="file-viewer-muted">No folders found.</p>
            ) : (
              filtered.map((folder) => (
                <button
                  key={folder.id}
                  type="button"
                  className={`file-viewer-folder${selectedId === folder.id ? " active" : ""}`}
                  onClick={() => setSelectedId(folder.id)}
                >
                  <FolderOpen size={15} aria-hidden="true" />
                  <span className="file-viewer-folder-name">{folder.name}</span>
                  <span className="file-viewer-folder-meta">{folder.page_count}</span>
                </button>
              ))
            )}
          </div>
        </aside>

        <section className="file-viewer-main pane" aria-label="Page viewer">
          {!selectedId ? (
            <div className="file-viewer-empty">
              <FolderOpen size={28} aria-hidden="true" />
              <p>Select a folder to open pages.</p>
            </div>
          ) : loadingDetail ? (
            <div className="file-viewer-empty">
              <p>Loading pages…</p>
            </div>
          ) : !detail || detail.pages.length === 0 ? (
            <div className="file-viewer-empty">
              <p>No pages in this folder.</p>
            </div>
          ) : source === "blob" && !blobReady ? (
            <div className="file-viewer-empty">
              <KeyRound size={28} aria-hidden="true" />
              {config?.blob_auth_mode === "entra" ? (
                <>
                  <p>Entra blob access is not configured on the server yet.</p>
                  <p className="file-viewer-muted">
                    Set BLOB_ACCOUNT_URL, BLOB_CONTAINER, and app registration or Managed Identity in .env.
                  </p>
                </>
              ) : (
                <>
                  <p>Authenticate with a SAS token to view blob pages.</p>
                  <button type="button" className="ocr-footer-btn primary" onClick={requestBlobMode}>
                    Authenticate
                  </button>
                </>
              )}
            </div>
          ) : (
            <>
              <div className="pane-header">
                <h2>Page{page ? ` · ${page.filename}` : ""}</h2>
                <div className="page-toolbar">
                  <div className="zoom-controls" role="group" aria-label="Zoom">
                    <button
                      type="button"
                      onClick={() => zoomBy(-ZOOM_STEP)}
                      disabled={zoom <= ZOOM_MIN}
                      aria-label="Zoom out"
                      title="Zoom out"
                    >
                      <ZoomOut size={15} />
                    </button>
                    <button
                      type="button"
                      className="zoom-reset"
                      onClick={resetZoom}
                      title="Reset zoom"
                    >
                      {Math.round(zoom * 100)}%
                    </button>
                    <button
                      type="button"
                      onClick={() => zoomBy(ZOOM_STEP)}
                      disabled={zoom >= ZOOM_MAX}
                      aria-label="Zoom in"
                      title="Zoom in"
                    >
                      <ZoomIn size={15} />
                    </button>
                  </div>
                  <button
                    type="button"
                    className="fullscreen-btn"
                    onClick={() => void toggleFullscreen()}
                    aria-label={isFullscreen ? "Exit fullscreen" : "Fullscreen"}
                    title={isFullscreen ? "Exit fullscreen" : "Fullscreen"}
                  >
                    {isFullscreen ? <Minimize2 size={15} /> : <Maximize2 size={15} />}
                  </button>
                  <div className="pager-nav">
                    <button
                      type="button"
                      disabled={pageIndex <= 0}
                      onClick={() => {
                        setPageIndex((i) => Math.max(0, i - 1));
                        resetZoom();
                      }}
                      aria-label="Previous page"
                    >
                      <ChevronLeft size={16} />
                    </button>
                    <PageJump
                      pageIndex={pageIndex}
                      pageCount={pageCount}
                      onJump={(idx) => {
                        setPageIndex(idx);
                        resetZoom();
                      }}
                    />
                    <button
                      type="button"
                      disabled={pageIndex >= pageCount - 1}
                      onClick={() => {
                        setPageIndex((i) => Math.min(pageCount - 1, i + 1));
                        resetZoom();
                      }}
                      aria-label="Next page"
                    >
                      <ChevronRight size={16} />
                    </button>
                  </div>
                </div>
              </div>

              <div className="file-viewer-stage-row">
                <div
                  className={`page-stage${isFullscreen ? " is-fullscreen" : ""}${stageClassName ? ` ${stageClassName}` : ""}`}
                  ref={pageStageRef}
                  {...stageProps}
                >
                  {page ? (
                    <img
                      src={imageSrc(page.page_number, page.filename)}
                      alt={page.filename}
                      draggable={false}
                      onPointerDown={stageProps.onPointerDown}
                      style={imageStyle}
                    />
                  ) : (
                    <div className="ocr-empty">No page selected</div>
                  )}
                  {isFullscreen ? (
                    <FullscreenPageChrome
                      pageIndex={pageIndex}
                      pageCount={pageCount}
                      zoom={zoom}
                      zoomMin={ZOOM_MIN}
                      zoomMax={ZOOM_MAX}
                      label={page?.filename}
                      onZoomOut={() => zoomBy(-ZOOM_STEP)}
                      onZoomIn={() => zoomBy(ZOOM_STEP)}
                      onZoomReset={resetZoom}
                      onPrev={() => {
                        setPageIndex((i) => Math.max(0, i - 1));
                        resetZoom();
                      }}
                      onNext={() => {
                        setPageIndex((i) => Math.min(pageCount - 1, i + 1));
                        resetZoom();
                      }}
                      onJump={(idx) => {
                        setPageIndex(idx);
                        resetZoom();
                      }}
                      onExitFullscreen={exitFullscreen}
                    />
                  ) : null}
                </div>

                {detail.pages.length > 0 && (
                  <div
                    className="filmstrip filmstrip-vertical"
                    role="listbox"
                    aria-label="Page thumbnails"
                  >
                    {detail.pages.map((p, idx) => (
                      <button
                        key={p.filename}
                        type="button"
                        className={`filmstrip-thumb${idx === pageIndex ? " active" : ""}`}
                        onClick={() => {
                          setPageIndex(idx);
                          setZoom(1);
                        }}
                        aria-label={`Go to ${p.filename}`}
                        aria-selected={idx === pageIndex}
                        title={p.filename}
                      >
                        <img
                          src={imageSrc(p.page_number, p.filename)}
                          alt=""
                          loading="lazy"
                        />
                      </button>
                    ))}
                  </div>
                )}
              </div>
            </>
          )}
        </section>
      </div>

      {showBlobAuth && config ? (
        <BlobAuthModal
          accountUrl={config.blob_account_url}
          container={config.blob_container}
          onCancel={() => setShowBlobAuth(false)}
          onAuthenticated={() => {
            setShowBlobAuth(false);
            setBlobReady(true);
            setSource("blob");
          }}
        />
      ) : null}
    </div>
  );
}
