import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type PointerEvent as ReactPointerEvent,
} from "react";

type Pan = { x: number; y: number };

/**
 * Drag-to-pan when zoomed (>100%). Wire stageProps onto the page-stage
 * and imageStyle onto the image (or a pan wrapper).
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
    if (zoom <= 1) resetPan();
  }, [zoom, resetPan]);

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

  const onPointerDown = useCallback(
    (e: ReactPointerEvent<HTMLElement>) => {
      if (zoom <= 1 || e.button !== 0) return;
      const t = e.target as HTMLElement | null;
      if (t?.closest?.(".fs-chrome, button, a, input")) return;

      e.preventDefault();
      e.stopPropagation();
      draggingRef.current = true;
      lastRef.current = { x: e.clientX, y: e.clientY };
      setDragging(true);
    },
    [zoom],
  );

  const canPan = zoom > 1;

  return {
    pan,
    dragging,
    canPan,
    resetPan,
    imageStyle: {
      transform: `translate3d(${pan.x}px, ${pan.y}px, 0) scale(${zoom})`,
      transformOrigin: "center center",
      cursor: canPan ? (dragging ? "grabbing" : "grab") : undefined,
      transition: dragging || canPan ? "none" : "transform 0.12s ease",
    } as const,
    stageProps: {
      onPointerDown,
      role: canPan ? ("application" as const) : undefined,
      "aria-label": canPan ? "Zoomed page — drag to pan" : undefined,
      title: canPan ? "Drag to pan" : undefined,
    },
    stageClassName: [
      canPan ? "is-zoom-pannable" : "",
      dragging ? "is-panning" : "",
    ]
      .filter(Boolean)
      .join(" "),
  };
}
