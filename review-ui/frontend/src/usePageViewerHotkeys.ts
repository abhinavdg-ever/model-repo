import { useEffect } from "react";

type Options = {
  enabled?: boolean;
  pageCount: number;
  setPageIndex: (updater: (i: number) => number) => void;
  zoomBy: (delta: number) => void;
  setZoom: (zoom: number) => void;
  zoomStep: number;
  isFullscreen: boolean;
  onExitFullscreen?: () => void;
};

function isTypingTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  const tag = target.tagName;
  if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return true;
  return target.isContentEditable;
}

/** Arrow keys for pages; +/- / 0 for zoom; Esc exits fullscreen. */
export function usePageViewerHotkeys({
  enabled = true,
  pageCount,
  setPageIndex,
  zoomBy,
  setZoom,
  zoomStep,
  isFullscreen,
  onExitFullscreen,
}: Options) {
  useEffect(() => {
    if (!enabled || pageCount <= 0) return;

    function onKey(e: KeyboardEvent) {
      if (isTypingTarget(e.target)) return;

      switch (e.key) {
        case "ArrowLeft":
        case "ArrowUp":
          e.preventDefault();
          setPageIndex((i) => Math.max(0, i - 1));
          setZoom(1);
          break;
        case "ArrowRight":
        case "ArrowDown":
          e.preventDefault();
          setPageIndex((i) => Math.min(pageCount - 1, i + 1));
          setZoom(1);
          break;
        case "+":
        case "=":
          e.preventDefault();
          zoomBy(zoomStep);
          break;
        case "-":
        case "_":
          e.preventDefault();
          zoomBy(-zoomStep);
          break;
        case "0":
          e.preventDefault();
          setZoom(1);
          break;
        case "Escape":
          if (isFullscreen) {
            e.preventDefault();
            onExitFullscreen?.();
          }
          break;
        default:
          break;
      }
    }

    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [
    enabled,
    pageCount,
    setPageIndex,
    zoomBy,
    setZoom,
    zoomStep,
    isFullscreen,
    onExitFullscreen,
  ]);
}
