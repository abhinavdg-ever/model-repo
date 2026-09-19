import { useEffect, useRef, useState, type PointerEvent as ReactPointerEvent } from "react";
import {
  ChevronLeft,
  ChevronRight,
  GripHorizontal,
  Minimize2,
  ZoomIn,
  ZoomOut,
} from "lucide-react";
import PageJump from "./PageJump";

type Props = {
  pageIndex: number;
  pageCount: number;
  zoom: number;
  zoomMin: number;
  zoomMax: number;
  onZoomOut: () => void;
  onZoomIn: () => void;
  onZoomReset: () => void;
  onPrev: () => void;
  onNext: () => void;
  onJump: (pageIndex: number) => void;
  onExitFullscreen: () => void;
  label?: string;
};

type Pos = { x: number; y: number };

/** Floating controls shown while the page stage is fullscreen (draggable). */
export default function FullscreenPageChrome({
  pageIndex,
  pageCount,
  zoom,
  zoomMin,
  zoomMax,
  onZoomOut,
  onZoomIn,
  onZoomReset,
  onPrev,
  onNext,
  onJump,
  onExitFullscreen,
  label,
}: Props) {
  const barRef = useRef<HTMLDivElement>(null);
  const dragRef = useRef<{
    pointerId: number;
    startX: number;
    startY: number;
    origX: number;
    origY: number;
  } | null>(null);
  const [pos, setPos] = useState<Pos | null>(null);
  const [dragging, setDragging] = useState(false);

  // Default: centered near bottom of the fullscreen stage
  useEffect(() => {
    const el = barRef.current;
    const parent = el?.offsetParent as HTMLElement | null;
    if (!el || !parent) return;
    const x = Math.max(8, (parent.clientWidth - el.offsetWidth) / 2);
    const y = Math.max(8, parent.clientHeight - el.offsetHeight - 24);
    setPos({ x, y });
  }, []);

  function clamp(next: Pos): Pos {
    const el = barRef.current;
    const parent = el?.offsetParent as HTMLElement | null;
    if (!el || !parent) return next;
    const maxX = Math.max(8, parent.clientWidth - el.offsetWidth - 8);
    const maxY = Math.max(8, parent.clientHeight - el.offsetHeight - 8);
    return {
      x: Math.min(maxX, Math.max(8, next.x)),
      y: Math.min(maxY, Math.max(8, next.y)),
    };
  }

  function onPointerDown(e: ReactPointerEvent) {
    if (!pos) return;
    // Only drag from the handle / bar chrome, not from buttons
    const target = e.target as HTMLElement;
    if (target.closest("button")) return;

    e.preventDefault();
    e.currentTarget.setPointerCapture(e.pointerId);
    dragRef.current = {
      pointerId: e.pointerId,
      startX: e.clientX,
      startY: e.clientY,
      origX: pos.x,
      origY: pos.y,
    };
    setDragging(true);
  }

  function onPointerMove(e: ReactPointerEvent) {
    const drag = dragRef.current;
    if (!drag || drag.pointerId !== e.pointerId) return;
    const dx = e.clientX - drag.startX;
    const dy = e.clientY - drag.startY;
    setPos(clamp({ x: drag.origX + dx, y: drag.origY + dy }));
  }

  function onPointerUp(e: ReactPointerEvent) {
    const drag = dragRef.current;
    if (!drag || drag.pointerId !== e.pointerId) return;
    dragRef.current = null;
    setDragging(false);
    try {
      e.currentTarget.releasePointerCapture(e.pointerId);
    } catch {
      /* ignore */
    }
  }

  return (
    <div
      ref={barRef}
      className={`fs-chrome${dragging ? " dragging" : ""}`}
      role="toolbar"
      aria-label="Fullscreen page controls"
      style={
        pos
          ? { left: pos.x, top: pos.y, bottom: "auto", transform: "none" }
          : undefined
      }
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onPointerCancel={onPointerUp}
    >
      <span className="fs-chrome-handle" title="Drag to move" aria-hidden="true">
        <GripHorizontal size={16} />
      </span>

      <div className="fs-chrome-group">
        <button type="button" onClick={onZoomOut} disabled={zoom <= zoomMin} aria-label="Zoom out" title="Zoom out (-)">
          <ZoomOut size={16} />
        </button>
        <button type="button" className="zoom-reset" onClick={onZoomReset} title="Reset zoom (0)">
          {Math.round(zoom * 100)}%
        </button>
        <button type="button" onClick={onZoomIn} disabled={zoom >= zoomMax} aria-label="Zoom in" title="Zoom in (+)">
          <ZoomIn size={16} />
        </button>
      </div>

      <div className="fs-chrome-group">
        <button type="button" onClick={onPrev} disabled={pageIndex <= 0} aria-label="Previous page" title="Previous (←)">
          <ChevronLeft size={16} />
        </button>
        <PageJump
          pageIndex={pageIndex}
          pageCount={pageCount}
          onJump={onJump}
          className="fs-chrome-label"
          suffix={label ? ` · ${label}` : undefined}
        />
        <button
          type="button"
          onClick={onNext}
          disabled={pageIndex >= pageCount - 1}
          aria-label="Next page"
          title="Next (→)"
        >
          <ChevronRight size={16} />
        </button>
      </div>

      <button
        type="button"
        className="fs-chrome-exit"
        onClick={onExitFullscreen}
        aria-label="Exit fullscreen"
        title="Exit fullscreen (Esc)"
      >
        <Minimize2 size={16} />
        Exit
      </button>
    </div>
  );
}
