import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type PointerEvent as ReactPointerEvent,
} from "react";

type Pan = { x: number; y: number };

/**
 * Drag-to-pan for the page image (normal pane and fullscreen).
 * Works at any zoom — including 100% — so the split viewer matches fullscreen.
 * Wire stageProps onto the page-stage (and wrap if overlays sit above the img)
 * and imageStyle onto the image or pan wrapper.
 */
export function useImagePan(zoom: number, resetKey?: string | number) {
  const [pan, setPan] = useState<Pan>({ x: 0, y: 0 });
  const [dragging, setDragging] = useState(false);
  const panRef = useRef<Pan>({ x: 0, y: 0 });
  const lastRef = useRef<Pan>({ x: 0, y: 0 });
  const draggingRef = useRef(false);

  const syncPan = useCallback((next: Pan) => {
    panRef.current = next;
    setPan(next);
  }, []);

  const resetPan = useCallback(() => {
    syncPan({ x: 0, y: 0 });
  }, [syncPan]);

  useEffect(() => {
    resetPan();
  }, [resetKey, resetPan]);

  // Window-level move/up so drag keeps working even if the pointer leaves the stage
  useEffect(() => {
    if (!dragging) return;

    function onMove(e: PointerEvent) {
      if (!draggingRef.current) return;
      const dx = e.clientX - lastRef.current.x;
      const dy = e.clientY - lastRef.current.y;
      lastRef.current = { x: e.clientX, y: e.clientY };
      syncPan({
        x: panRef.current.x + dx,
        y: panRef.current.y + dy,
      });
    }

    function onUp() {
      draggingRef.current = false;
      setDragging(false);
    }

    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    window.addEventListener("pointercancel", onUp);
    return () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      window.removeEventListener("pointercancel", onUp);
    };
  }, [dragging, syncPan]);

  const onPointerDown = useCallback((e: ReactPointerEvent<HTMLElement>) => {
    if (e.button !== 0) return;
    const t = e.target as HTMLElement | null;
    if (t?.closest?.(".fs-chrome, button, a, input, .pager-jump")) return;

    e.preventDefault();
    e.stopPropagation();
    draggingRef.current = true;
    lastRef.current = { x: e.clientX, y: e.clientY };
    setDragging(true);
  }, []);

  return {
    pan,
    dragging,
    canPan: true,
    resetPan,
    imageStyle: {
      transform: `translate3d(${pan.x}px, ${pan.y}px, 0) scale(${zoom})`,
      transformOrigin: "center center",
      cursor: dragging ? "grabbing" : "grab",
      transition: dragging ? "none" : "transform 0.12s ease",
    } as const,
    stageProps: {
      onPointerDown,
      role: "application" as const,
      "aria-label": "Page image — drag to pan",
      title: "Drag to pan",
    },
    stageClassName: ["is-zoom-pannable", dragging ? "is-panning" : ""]
      .filter(Boolean)
      .join(" "),
  };
}
