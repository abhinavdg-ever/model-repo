import { useEffect, useMemo, useRef, useState } from "react";
import {
  ArrowLeft,
  ChevronLeft,
  ChevronRight,
  Copy,
  Download,
  FileText,
  Maximize2,
  Minimize2,
  PanelLeftClose,
  PanelLeftOpen,
  ScanSearch,
  ZoomIn,
  ZoomOut,
} from "lucide-react";
import {
  getFolder,
  getFolderImaging,
  getFolderOcr,
  OCR_TAB_LABELS,
  pageImageUrl,
  type FolderDetail,
  type ImagingDocumentResponse,
  type ImagingPageResult,
  type OcrKind,
  type OcrSectionHeader,
  type OutputMode,
} from "./api";
import ImagingPanel, { type ImagingTab } from "./ImagingPanel";
import { formatDuplicateLabel } from "./duplicateLabel";
import {
  formatMatchRatePercent,
  isUsableOcrPayload,
  matchRateTitle,
  matchRateToneClass,
  pageMatchRate,
} from "./ocrMatchRate";
import { ocrTextForFilename } from "./ocrPages";
import { prepareOcrLines } from "./ocrFormat";
import FullscreenPageChrome from "./FullscreenPageChrome";
import PageJump from "./PageJump";
import { useImagePan } from "./useImagePan";
import { usePageViewerHotkeys } from "./usePageViewerHotkeys";

type Props = {
  folderId: string;
  initialMode?: OutputMode;
  onBack: () => void;
  onModeChange?: (mode: OutputMode) => void;
};

const OCR_TABS: OcrKind[] = ["preliminary", "final1", "final2"];
const ZOOM_MIN = 0.5;
const ZOOM_MAX = 3;
const ZOOM_STEP = 0.25;

const KIND_FILE_SUFFIX: Record<OcrKind, string> = {
  preliminary: "prelim",
  final1: "final1",
  final2: "final2",
};

function downloadTextFile(filename: string, text: string, mime = "text/plain;charset=utf-8") {
  const blob = new Blob([text], { type: mime });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

function downloadJsonFile(filename: string, data: unknown) {
  downloadTextFile(filename, JSON.stringify(data, null, 2), "application/json;charset=utf-8");
}

/** Document-level DOS for download: prefer docDos*, else carry-forward / 2/2/2022 @ 80%. */
const DEFAULT_DOC_DOS = "2/2/2022";
const DEFAULT_DOC_DOS_CONFIDENCE = 0.8;

function fillDocDosForDownload(pages: ImagingPageResult[]): ImagingPageResult[] {
  let prevFrom: string | null = null;
  let prevTo: string | null = null;
  let prevConf: number | null = null;
  return [...pages]
    .sort((a, b) => a.pageNumber - b.pageNumber)
    .map((page) => {
      let dosFrom = (page.docDosFrom || page.dosFrom || "").trim() || null;
      let dosTo = (page.docDosTo || page.dosTo || "").trim() || null;
      let dosConfidence = page.dosConfidence ?? null;
      if (!dosFrom && !dosTo) {
        if (prevFrom) {
          dosFrom = prevFrom;
          dosTo = prevTo ?? prevFrom;
          dosConfidence = prevConf;
        } else {
          dosFrom = DEFAULT_DOC_DOS;
          dosTo = DEFAULT_DOC_DOS;
          dosConfidence = DEFAULT_DOC_DOS_CONFIDENCE;
        }
      } else {
        if (!dosFrom) dosFrom = dosTo ?? prevFrom ?? DEFAULT_DOC_DOS;
        if (!dosTo) dosTo = dosFrom ?? prevTo ?? DEFAULT_DOC_DOS;
        if (
          (dosFrom === DEFAULT_DOC_DOS || dosTo === DEFAULT_DOC_DOS) &&
          dosConfidence == null
        ) {
          dosConfidence = DEFAULT_DOC_DOS_CONFIDENCE;
        }
      }
      prevFrom = dosFrom;
      prevTo = dosTo;
      prevConf = dosConfidence ?? prevConf;
      return {
        ...page,
        docDosFrom: dosFrom,
        docDosTo: dosTo,
        dosConfidence,
      };
    });
}

function findImagingPage(
  doc: ImagingDocumentResponse | null,
  page: { page_number: number; filename: string } | null,
): ImagingPageResult | null {
  if (!doc || !page) return null;
  const byFile = doc.pages.find(
    (p) => p.fileName.toLowerCase() === page.filename.toLowerCase(),
  );
  if (byFile) return byFile;
  return doc.pages.find((p) => p.pageNumber === page.page_number) ?? null;
}

/** Azure Final2 is skipped for high-quality printed pages (billed stage). */
const FINAL2_QUALITY_SKIP_MESSAGE = "Skipped for High Quality Images";
const FINAL_OCR_BLANK_JUNK_SKIP_MESSAGE = "Skipped for Blank/Junk";

function isBlankOrJunkYes(page: ImagingPageResult | null): boolean {
  const v = (page?.blankOrJunk || "").trim().toLowerCase();
  return v.startsWith("yes");
}

/** Infer Final2 quality-skip from imaging when JSON has no skippedReason yet. */
function isFinal2QualitySkip(page: ImagingPageResult | null): boolean {
  if (!page || isBlankOrJunkYes(page)) return false;
  const hw = (page.handwrittenOrPrinted || "").trim().toLowerCase();
  const tag = (page.pageQualityTag || "").trim().toLowerCase();
  return hw === "printed" && tag === "high";
}

function isOcrSkipMessage(text: string): boolean {
  const t = text.trim();
  return (
    t === FINAL2_QUALITY_SKIP_MESSAGE ||
    t === FINAL_OCR_BLANK_JUNK_SKIP_MESSAGE ||
    t.startsWith("Skipped for ")
  );
}

export default function FolderViewer({
  folderId,
  initialMode = "ocr",
  onBack,
  onModeChange,
}: Props) {
  const [folder, setFolder] = useState<FolderDetail | null>(null);
  const [pageIndex, setPageIndex] = useState(0);
  const [outputMode, setOutputMode] = useState<OutputMode>(initialMode);
  const [ocrTab, setOcrTab] = useState<OcrKind>("preliminary");
  const [showSectionHeaders, setShowSectionHeaders] = useState(false);
  const [imagingTab, setImagingTab] = useState<ImagingTab>("page");
  const [ocrByKind, setOcrByKind] = useState<Partial<Record<OcrKind, string>>>({});
  const [headersByKind, setHeadersByKind] = useState<
    Partial<Record<OcrKind, Record<string, OcrSectionHeader[]>>>
  >({});
  const [imagingDoc, setImagingDoc] = useState<ImagingDocumentResponse | null>(null);
  const [loadingFolder, setLoadingFolder] = useState(true);
  const [loadingOcr, setLoadingOcr] = useState(initialMode === "ocr");
  const [loadingImaging, setLoadingImaging] = useState(initialMode === "imaging");
  const [imagingError, setImagingError] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [zoom, setZoom] = useState(1);
  const [isFullscreen, setIsFullscreen] = useState(false);
  const [outputExpanded, setOutputExpanded] = useState(false);
  const [imageNaturalSize, setImageNaturalSize] = useState<{ w: number; h: number } | null>(
    null,
  );
  const [pageImageLoading, setPageImageLoading] = useState(true);
  const [stageSize, setStageSize] = useState<{ w: number; h: number } | null>(null);
  const pageStageRef = useRef<HTMLDivElement>(null);
  const pageImageRef = useRef<HTMLImageElement>(null);
  const {
    resetPan,
    imageStyle,
    stageProps,
    stageClassName,
  } = useImagePan(zoom, `${folderId}:${pageIndex}`);

  function resetZoom() {
    setZoom(1);
    resetPan();
  }

  useEffect(() => {
    setImageNaturalSize(null);
    setPageImageLoading(true);
  }, [folderId, pageIndex]);

  // Cached images often skip onLoad — pick up natural size when the page flips.
  useEffect(() => {
    const img = pageImageRef.current;
    if (img?.complete && img.naturalWidth > 0) {
      setPageImageLoading(false);
      setImageNaturalSize({ w: img.naturalWidth, h: img.naturalHeight });
    }
  }, [folderId, pageIndex, folder]);

  // Fit the page image into the stage (document-processing pattern) so the
  // overlay container matches the displayed image — not a clipped wrap.
  useEffect(() => {
    const el = pageStageRef.current;
    if (!el) return;
    const measure = () => {
      const style = getComputedStyle(el);
      const padX =
        (parseFloat(style.paddingLeft) || 0) + (parseFloat(style.paddingRight) || 0);
      const padY =
        (parseFloat(style.paddingTop) || 0) + (parseFloat(style.paddingBottom) || 0);
      setStageSize({
        w: Math.max(0, el.clientWidth - padX),
        h: Math.max(0, el.clientHeight - padY),
      });
    };
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, [folderId, outputExpanded, isFullscreen]);

  const fittedImageSize = useMemo(() => {
    if (!imageNaturalSize?.w || !imageNaturalSize?.h || !stageSize?.w || !stageSize?.h) {
      return null;
    }
    const fit = Math.min(
      stageSize.w / imageNaturalSize.w,
      stageSize.h / imageNaturalSize.h,
    );
    return {
      w: Math.max(1, imageNaturalSize.w * fit),
      h: Math.max(1, imageNaturalSize.h * fit),
    };
  }, [imageNaturalSize, stageSize]);

  useEffect(() => {
    setOutputMode(initialMode);
  }, [initialMode]);

  useEffect(() => {
    let cancelled = false;
    setLoadingFolder(true);
    setError(null);
    setPageIndex(0);
    setOcrTab("preliminary");
    setImagingTab("page");
    setImagingDoc(null);
    setOcrByKind({});
    setHeadersByKind({});
    setZoom(1);
    getFolder(folderId)
      .then((data) => {
        if (!cancelled) setFolder(data);
      })
      .catch((err) => {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : "Failed to load folder");
        }
      })
      .finally(() => {
        if (!cancelled) setLoadingFolder(false);
      });
    return () => {
      cancelled = true;
    };
  }, [folderId]);

  useEffect(() => {
    function onFsChange() {
      const on = document.fullscreenElement === pageStageRef.current;
      setIsFullscreen(on);
      // Entering fullscreen always starts at 100%; zoom then persists across pages.
      if (on) {
        setZoom(1);
        resetPan();
      }
    }
    document.addEventListener("fullscreenchange", onFsChange);
    return () => document.removeEventListener("fullscreenchange", onFsChange);
  }, [resetPan]);

  function goToPage(idx: number | ((i: number) => number)) {
    setPageIndex(idx);
    // Outside fullscreen, new pages open at 100%. In fullscreen, keep the zoom.
    if (!isFullscreen) {
      resetZoom();
    }
  }

  const page = folder?.pages[pageIndex] ?? null;

  const ocrFetchedRef = useRef<Set<OcrKind>>(new Set());
  const imagingFetchedRef = useRef(false);

  useEffect(() => {
    // Reset payloads when the chart changes; OCR/imaging load only when that mode is open.
    setImagingDoc(null);
    setOcrByKind({});
    setHeadersByKind({});
    setImagingError(null);
    ocrFetchedRef.current = new Set();
    imagingFetchedRef.current = false;
    setLoadingImaging(outputMode === "imaging");
    // Only reset on chart change — keep cached imaging when flipping OCR ↔ Imaging.
    // eslint-disable-next-line react-hooks/exhaustive-deps -- outputMode read once per folderId
  }, [folderId]);

  useEffect(() => {
    if (!folder || outputMode !== "ocr") {
      return;
    }
    if (ocrFetchedRef.current.has(ocrTab)) {
      return;
    }
    ocrFetchedRef.current.add(ocrTab);
    let cancelled = false;
    setLoadingOcr(true);
    setCopied(false);

    getFolderOcr(folder.id, ocrTab)
      .then((data) => {
        if (cancelled) return;
        setOcrByKind((prev) => ({
          ...prev,
          [ocrTab]: data?.text?.trim() ? data.text : "",
        }));
        if (data?.section_headers_by_file) {
          setHeadersByKind((prev) => ({
            ...prev,
            [ocrTab]: data.section_headers_by_file,
          }));
        }
      })
      .catch(() => {
        if (!cancelled) {
          setOcrByKind((prev) => ({ ...prev, [ocrTab]: "" }));
        }
      })
      .finally(() => {
        if (!cancelled) setLoadingOcr(false);
      });

    return () => {
      cancelled = true;
    };
  }, [folder, outputMode, ocrTab]);

  const ocrFullText = ocrByKind[ocrTab] ?? "";
  const ocrMissingMessage = `No ${OCR_TAB_LABELS[ocrTab]} available.`;
  const sectionHeadersByFile = headersByKind[ocrTab] ?? {};

  useEffect(() => {
    if (!folder || outputMode !== "imaging" || imagingTab !== "additional") {
      return;
    }
    const needFinal1 = !ocrFetchedRef.current.has("final1");
    const needFinal2 = !ocrFetchedRef.current.has("final2");
    if (!needFinal1 && !needFinal2) {
      return;
    }
    const kinds = (
      [
        needFinal1 ? "final1" : null,
        needFinal2 ? "final2" : null,
      ] as const
    ).filter((k): k is "final1" | "final2" => k != null);
    for (const k of kinds) ocrFetchedRef.current.add(k);

    let cancelled = false;
    setLoadingOcr(true);
    Promise.all(
      kinds.map(async (kind) => {
        try {
          const data = await getFolderOcr(folder.id, kind);
          return [kind, data] as const;
        } catch {
          return [kind, null] as const;
        }
      }),
    )
      .then((entries) => {
        if (cancelled) return;
        setOcrByKind((prev) => {
          const next = { ...prev };
          for (const [kind, data] of entries) {
            if (next[kind] === undefined) {
              next[kind] = data?.text?.trim() ? data.text : "";
            }
          }
          return next;
        });
        setHeadersByKind((prev) => {
          const next = { ...prev };
          for (const [kind, data] of entries) {
            if (data?.section_headers_by_file) {
              next[kind] = data.section_headers_by_file;
            } else if (next[kind] === undefined) {
              next[kind] = {};
            }
          }
          return next;
        });
      })
      .finally(() => {
        if (!cancelled) setLoadingOcr(false);
      });
    return () => {
      cancelled = true;
    };
  }, [folder, outputMode, imagingTab]);

  useEffect(() => {
    if (!folder || outputMode !== "imaging") {
      return;
    }
    if (imagingFetchedRef.current) {
      return;
    }
    imagingFetchedRef.current = true;
    let cancelled = false;
    setLoadingImaging(true);
    setImagingError(null);
    getFolderImaging(folder.id)
      .then((data) => {
        if (!cancelled) setImagingDoc(data);
      })
      .catch((err) => {
        if (!cancelled) {
          setImagingDoc(null);
          setImagingError(
            err instanceof Error ? err.message : "Failed to load imaging results",
          );
        }
      })
      .finally(() => {
        if (!cancelled) setLoadingImaging(false);
      });
    return () => {
      cancelled = true;
    };
  }, [folder, outputMode]);

  const imagingPage = useMemo(
    () => findImagingPage(imagingDoc, page),
    [imagingDoc, page],
  );

  // One full-screen gate until folder + mode payload + first page image are ready.
  const bootComplete = useMemo(() => {
    if (error) return true;
    if (loadingFolder || !folder) return false;
    if (outputMode === "imaging") {
      if (loadingImaging || (!imagingDoc && !imagingError)) return false;
    } else if (loadingOcr || ocrByKind[ocrTab] === undefined) {
      return false;
    }
    if (folder.pages.length > 0 && pageImageLoading) return false;
    return true;
  }, [
    error,
    loadingFolder,
    folder,
    outputMode,
    loadingImaging,
    imagingDoc,
    imagingError,
    loadingOcr,
    ocrByKind,
    ocrTab,
    pageImageLoading,
  ]);
  const [bootDone, setBootDone] = useState(false);
  useEffect(() => {
    setBootDone(false);
  }, [folderId]);
  useEffect(() => {
    if (bootComplete) setBootDone(true);
  }, [bootComplete]);
  const showBootOverlay = !bootDone && !error;

  const pageOcrText = useMemo(() => {
    if (loadingOcr) return "";
    if (!page) return "";

    const resolveSkip = (chunk: string): string | null => {
      const t = chunk.trim();
      if (t === FINAL2_QUALITY_SKIP_MESSAGE) return FINAL2_QUALITY_SKIP_MESSAGE;
      if (t === FINAL_OCR_BLANK_JUNK_SKIP_MESSAGE) {
        return FINAL_OCR_BLANK_JUNK_SKIP_MESSAGE;
      }
      if (t.startsWith("Skipped for ")) return t;
      return null;
    };

    // Current imaging wins over leftover OCR files (e.g. skip_ocr kept
    // final2.json while quality re-ran to High+Printed, or junk flipped).
    if (ocrTab === "final1" || ocrTab === "final2") {
      if (isBlankOrJunkYes(imagingPage)) {
        return FINAL_OCR_BLANK_JUNK_SKIP_MESSAGE;
      }
      if (ocrTab === "final2" && isFinal2QualitySkip(imagingPage)) {
        return FINAL2_QUALITY_SKIP_MESSAGE;
      }
    }

    if ((ocrTab === "final1" || ocrTab === "final2") && ocrFullText) {
      const chunk = ocrTextForFilename(ocrFullText, page.filename);
      const skip = resolveSkip(chunk);
      if (skip) return skip;
      if (chunk.trim()) return chunk;
      return `No OCR text found for ${page.filename}.`;
    }

    if (ocrTab === "final2" && page.has_final2_ocr && !ocrFullText) {
      return `No OCR text found for ${page.filename}.`;
    }

    if (!ocrFullText) return ocrMissingMessage;
    if (!isUsableOcrPayload(ocrFullText)) return ocrFullText;
    const chunk = ocrTextForFilename(ocrFullText, page.filename);
    return chunk || `No OCR text found for ${page.filename}.`;
  }, [
    loadingOcr,
    ocrFullText,
    ocrMissingMessage,
    page,
    ocrTab,
    imagingPage,
  ]);

  const showHeaderToggle = ocrTab === "final1" || ocrTab === "final2";

  const pageHeaderBoxes = useMemo(() => {
    if (!showSectionHeaders || !page) return [];
    // Don't overlay headers from a Final2 file the UI is treating as skipped.
    if (ocrTab === "final2" && isFinal2QualitySkip(imagingPage)) return [];
    if (
      (ocrTab === "final1" || ocrTab === "final2") &&
      isBlankOrJunkYes(imagingPage)
    ) {
      return [];
    }
    return (
      sectionHeadersByFile[page.filename] ??
      sectionHeadersByFile[page.filename.toLowerCase()] ??
      []
    );
  }, [showSectionHeaders, page, sectionHeadersByFile, ocrTab, imagingPage]);

  /** Imaging → Section coordinates: Final2 first, else Final1; skip blank/junk. */
  const imagingSectionInfo = useMemo(() => {
    if (!page) {
      return {
        headers: [] as OcrSectionHeader[],
        source: null as "final1" | "final2" | null,
        skipped: false,
      };
    }
    if (isBlankOrJunkYes(imagingPage)) {
      return { headers: [] as OcrSectionHeader[], source: null, skipped: true };
    }
    const pick = (kind: "final1" | "final2"): OcrSectionHeader[] => {
      const byFile = headersByKind[kind] ?? {};
      return (
        byFile[page.filename] ??
        byFile[page.filename.toLowerCase()] ??
        []
      );
    };
    // High+Printed → Final2 was (or should be) skipped; prefer Final1 coords.
    const preferFinal1 = isFinal2QualitySkip(imagingPage);
    if (!preferFinal1) {
      const f2 = pick("final2");
      if (f2.length > 0) {
        return { headers: f2, source: "final2" as const, skipped: false };
      }
    }
    const f1 = pick("final1");
    if (f1.length > 0) {
      return { headers: f1, source: "final1" as const, skipped: false };
    }
    return { headers: [] as OcrSectionHeader[], source: null, skipped: false };
  }, [page, imagingPage, headersByKind]);

  const overlayBoxes = useMemo(() => {
    if (outputMode === "imaging" && imagingTab === "additional") {
      if (imagingSectionInfo.skipped) return [];
      return imagingSectionInfo.headers.filter(
        (b) => b.width > 0 && b.height > 0,
      );
    }
    if (outputMode === "ocr") {
      return pageHeaderBoxes.filter((b) => b.width > 0 && b.height > 0);
    }
    return [];
  }, [
    outputMode,
    imagingTab,
    imagingSectionInfo,
    pageHeaderBoxes,
  ]);

  const pageOcrLines = useMemo(() => {
    if (!showHeaderToggle || !pageOcrText || pageOcrText === ocrMissingMessage) {
      return null;
    }
    if (pageOcrText.startsWith("No OCR text found")) return null;
    if (isOcrSkipMessage(pageOcrText)) return null;
    const known = pageHeaderBoxes.map((h) => h.text);
    return prepareOcrLines(pageOcrText, {
      showSectionHeaders,
      // Only lines that survived the ≥90% canon match (from the API).
      detectPlainHeaders: false,
      knownHeaders: known,
    });
  }, [
    showHeaderToggle,
    pageOcrText,
    ocrMissingMessage,
    showSectionHeaders,
    pageHeaderBoxes,
  ]);

  const ocrMatch = useMemo(() => {
    if (!page) {
      return { rate: null, count: 0, engines: [], pairs: [] };
    }
    return pageMatchRate(ocrByKind, page.filename, OCR_TABS);
  }, [ocrByKind, page]);

  const pageQualityLabel = useMemo(() => {
    const raw = (imagingPage?.pageQualityTag || "").trim().toLowerCase();
    if (raw === "high" || raw === "medium" || raw === "low") {
      return raw.charAt(0).toUpperCase() + raw.slice(1);
    }
    return null;
  }, [imagingPage]);

  const canUseFull = isUsableOcrPayload(ocrFullText);
  const pageCount = folder?.pages.length ?? 0;
  const folderName = folder?.name ?? folderId;
  const suffix = KIND_FILE_SUFFIX[ocrTab];

  function changeMode(mode: OutputMode) {
    if (mode === "imaging" && !imagingDoc && !imagingError) {
      setLoadingImaging(true);
    }
    if (mode === "ocr" && ocrByKind[ocrTab] === undefined) {
      setLoadingOcr(true);
    }
    setOutputMode(mode);
    onModeChange?.(mode);
  }

  function downloadFullOcr() {
    if (!canUseFull) return;
    downloadTextFile(`${folderName}_${suffix}.txt`, ocrFullText);
  }

  async function copyFullOcr() {
    if (!canUseFull) return;
    try {
      await navigator.clipboard.writeText(ocrFullText);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1600);
    } catch {
      setCopied(false);
    }
  }

  function downloadImagingDocJson() {
    if (!imagingDoc) return;
    const status =
      imagingDoc.verifications?.[0]?.finalStatus ??
      imagingDoc.verification?.finalStatus ??
      "";
    const pages = fillDocDosForDownload(imagingDoc.pages).map((p) => ({
      chartName: folderName,
      pageName: p.fileName,
      memberName: p.memberName,
      memberID: p.memberId,
      confidence: p.memberConfidence,
      memberDob: p.memberDob,
      handwrittenOrPrinted: p.handwrittenOrPrinted,
      handwrittenOrPrintedConfidence: p.handwrittenOrPrintedConfidence ?? null,
      orientationAngle: p.orientationAngle,
      tiltAngle: p.tiltAngle,
      mirrored: p.mirrored,
      pageQualityTag: p.pageQualityTag ?? null,
      pageQualityConfidence: p.pageQualityConfidence,
      blankOrJunk: p.blankOrJunk ?? null,
      isDuplicate: p.isDuplicate ?? null,
      pageType: p.pageType,
      pageTypeConfidence: p.pageTypeConfidence,
      isCodeable: p.isCodeable ?? null,
      currentSequence: p.currentSequence ?? p.pageNumber,
      actualSequence: p.actualSequence ?? null,
      encounterType: p.encounterType ?? null,
      dosFrom: p.docDosFrom ?? p.dosFrom,
      dosTo: p.docDosTo ?? p.dosTo,
      dosConfidence: p.dosConfidence ?? null,
      member_verification_status: status,
    }));
    downloadJsonFile(`${folderName}_imaging.json`, pages);
  }

  function downloadImagingDocCsv() {
    if (!imagingDoc) return;
    const headers = [
      "chartName",
      "pageName",
      "memberName",
      "memberID",
      "confidence",
      "memberDob",
      "handwrittenOrPrinted",
      "handwrittenOrPrintedConfidence",
      "orientationAngle",
      "tiltAngle",
      "mirrored",
      "pageQualityTag",
      "pageQualityConfidence",
      "blankOrJunk",
      "isDuplicate",
      "pageType",
      "pageTypeConfidence",
      "isCodeable",
      "currentSequence",
      "actualSequence",
      "encounterType",
      "dosFrom",
      "dosTo",
      "dosConfidence",
      "member_verification_status",
    ];
    const esc = (v: unknown) => {
      const s = v === null || v === undefined ? "" : String(v);
      if (/[",\n\r]/.test(s)) return `"${s.replace(/"/g, '""')}"`;
      return s;
    };
    const status =
      imagingDoc.verifications?.[0]?.finalStatus ??
      imagingDoc.verification?.finalStatus ??
      "";
    const rows = fillDocDosForDownload(imagingDoc.pages).map((p) =>
      [
        folderName,
        p.fileName,
        p.memberName,
        p.memberId,
        p.memberConfidence,
        p.memberDob,
        p.handwrittenOrPrinted,
        p.handwrittenOrPrintedConfidence ?? "",
        p.orientationAngle,
        p.tiltAngle,
        p.mirrored,
        p.pageQualityTag ?? "",
        p.pageQualityConfidence,
        p.blankOrJunk ?? "NA",
        p.isDuplicate == null
          ? "NA"
          : formatDuplicateLabel(p.isDuplicate, p.pageTypeConfidence),
        p.pageType ?? "Not Available",
        p.pageTypeConfidence,
        p.isCodeable ?? "",
        p.currentSequence ?? p.pageNumber,
        p.actualSequence ?? "",
        p.encounterType ?? "",
        p.docDosFrom ?? p.dosFrom,
        p.docDosTo ?? p.dosTo,
        p.dosConfidence ?? "",
        status,
      ]
        .map(esc)
        .join(","),
    );
    downloadTextFile(
      `${folderName}_imaging.csv`,
      [headers.join(","), ...rows].join("\n"),
      "text/csv;charset=utf-8",
    );
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
        setZoom(1);
        resetPan();
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
    enabled: pageCount > 0,
    pageCount,
    setPageIndex: goToPage,
    zoomBy,
    setZoom,
    zoomStep: ZOOM_STEP,
    isFullscreen,
    onExitFullscreen: exitFullscreen,
  });

  return (
    <div className="workspace">
      {showBootOverlay ? (
        <div className="chart-boot-overlay" role="status" aria-live="polite" aria-busy="true">
          <div className="chart-boot-card">
            <span className="imaging-loading-spinner chart-boot-spinner" aria-hidden="true" />
            <p className="chart-boot-title">Loading results…</p>
            <p className="chart-boot-sub">{folder?.name ?? folderId}</p>
          </div>
        </div>
      ) : null}
      <div className="workspace-header">
        <div className="workspace-header-start">
          <button type="button" className="back-btn" onClick={onBack}>
            <ArrowLeft size={15} aria-hidden="true" />
            Back
          </button>
          <div className="workspace-title">
            <h1>{folder?.name ?? folderId}</h1>
            <p>
              {loadingFolder
                ? "Loading…"
                : `${pageCount} page${pageCount === 1 ? "" : "s"} · OCR ${folder?.ocr_processed ?? 0} · Imaging ${folder?.imaging_processed ?? 0}`}
            </p>
          </div>
        </div>

        <div className="mode-icon-group" role="group" aria-label="Output mode">
          <button
            type="button"
            className={`mode-icon-btn${outputMode === "ocr" ? " active" : ""}`}
            onClick={() => changeMode("ocr")}
            title="OCR"
            aria-label="OCR output"
            aria-pressed={outputMode === "ocr"}
          >
            <FileText size={18} aria-hidden="true" />
            <span>OCR</span>
          </button>
          <button
            type="button"
            className={`mode-icon-btn${outputMode === "imaging" ? " active" : ""}`}
            onClick={() => changeMode("imaging")}
            title="Imaging"
            aria-label="Imaging output"
            aria-pressed={outputMode === "imaging"}
          >
            <ScanSearch size={18} aria-hidden="true" />
            <span>Imaging</span>
          </button>
        </div>
      </div>

      {error && <div className="error-banner">{error}</div>}

      {!error && (
        <div className={`review-split${outputExpanded ? " output-expanded" : ""}`}>
          {!outputExpanded && (
          <section className="pane" aria-label="Page viewer">
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
                    onClick={() => goToPage((i) => Math.max(0, i - 1))}
                    aria-label="Previous page"
                  >
                    <ChevronLeft size={16} />
                  </button>
                  <PageJump
                    pageIndex={pageIndex}
                    pageCount={pageCount}
                    onJump={(idx) => goToPage(idx)}
                  />
                  <button
                    type="button"
                    disabled={pageIndex >= pageCount - 1}
                    onClick={() => goToPage((i) => Math.min(pageCount - 1, i + 1))}
                    aria-label="Next page"
                  >
                    <ChevronRight size={16} />
                  </button>
                </div>
              </div>
            </div>
            <div
              className={`page-stage${isFullscreen ? " is-fullscreen" : ""}${stageClassName ? ` ${stageClassName}` : ""}`}
              ref={pageStageRef}
              {...stageProps}
            >
              {page ? (
                <>
                  {pageImageLoading && !fittedImageSize ? (
                    <div className="ocr-loading page-image-loading" aria-live="polite">
                      Loading page…
                    </div>
                  ) : null}
                  <div
                    className="page-image-wrap"
                    style={{
                      ...(fittedImageSize
                        ? { width: fittedImageSize.w, height: fittedImageSize.h }
                        : { visibility: "hidden", width: 1, height: 1 }),
                      ...imageStyle,
                    }}
                    onPointerDown={stageProps.onPointerDown}
                  >
                    <img
                      ref={pageImageRef}
                      className="page-image"
                      src={pageImageUrl(folderId, page.page_number)}
                      alt={page.filename}
                      draggable={false}
                      onLoad={(e) => {
                        const img = e.currentTarget;
                        setPageImageLoading(false);
                        setImageNaturalSize({
                          w: img.naturalWidth,
                          h: img.naturalHeight,
                        });
                      }}
                      onError={() => setPageImageLoading(false)}
                    />
                    {overlayBoxes.length > 0 && fittedImageSize ? (
                      <div className="page-header-overlay" aria-hidden="true">
                        {overlayBoxes.map((box, i) => (
                          <div
                            key={`${box.text}-${i}`}
                            className="page-header-box"
                            title={box.text}
                            style={{
                              left: `${box.left * 100}%`,
                              top: `${box.top * 100}%`,
                              width: `${box.width * 100}%`,
                              height: `${Math.max(box.height * 100, 0.35)}%`,
                            }}
                          />
                        ))}
                      </div>
                    ) : null}
                  </div>
                </>
              ) : (
                <div className="ocr-empty">
                  {loadingFolder ? "Loading pages…" : "No pages in this folder"}
                </div>
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
                  onPrev={() => goToPage((i) => Math.max(0, i - 1))}
                  onNext={() => goToPage((i) => Math.min(pageCount - 1, i + 1))}
                  onJump={(idx) => goToPage(idx)}
                  onExitFullscreen={exitFullscreen}
                />
              ) : null}
            </div>
            {bootDone && folder && folder.pages.length > 0 && (
              <div className="filmstrip" role="listbox" aria-label="Page thumbnails">
                {folder.pages.map((p, idx) => (
                  <button
                    key={p.filename}
                    type="button"
                    className={`filmstrip-thumb${idx === pageIndex ? " active" : ""}`}
                    onClick={() => goToPage(idx)}
                    aria-label={`Go to ${p.filename}`}
                    aria-selected={idx === pageIndex}
                    title={p.filename}
                  >
                    <img
                      src={pageImageUrl(folderId, p.page_number, { thumb: true })}
                      alt=""
                      loading="lazy"
                      decoding="async"
                    />
                  </button>
                ))}
              </div>
            )}
          </section>
          )}

          <section className="pane" aria-label="Output panel">
            <div className="pane-header pane-header-wrap">
              <div className="pane-header-main">
                <button
                  type="button"
                  className="pane-expand-btn"
                  onClick={() => setOutputExpanded((v) => !v)}
                  title={
                    outputExpanded
                      ? "Show page viewer"
                      : "Expand output to full width"
                  }
                  aria-label={
                    outputExpanded
                      ? "Show page viewer"
                      : "Expand output to full width"
                  }
                  aria-pressed={outputExpanded}
                >
                  {outputExpanded ? (
                    <PanelLeftOpen size={16} aria-hidden="true" />
                  ) : (
                    <PanelLeftClose size={16} aria-hidden="true" />
                  )}
                </button>
                <h2>
                  {outputMode === "ocr" ? "OCR Output" : "Imaging Output"}
                  {page && outputMode === "ocr" ? (
                    <span className="ocr-page-label"> · {page.filename}</span>
                  ) : null}
                </h2>
              </div>
              {outputMode === "ocr" && (
                <div className="ocr-toolbar-row">
                  <div className="output-tabs" role="tablist" aria-label="OCR views">
                    {OCR_TABS.map((kind) => (
                      <button
                        key={kind}
                        type="button"
                        role="tab"
                        aria-selected={ocrTab === kind}
                        className={ocrTab === kind ? "active" : ""}
                        onClick={() => setOcrTab(kind)}
                        disabled={loadingOcr ? false : !ocrByKind[kind]}
                        title={
                          ocrByKind[kind]
                            ? OCR_TAB_LABELS[kind]
                            : `${OCR_TAB_LABELS[kind]} unavailable`
                        }
                      >
                        {OCR_TAB_LABELS[kind]}
                      </button>
                    ))}
                  </div>
                  <div
                    className={`ocr-match-rate ${
                      loadingOcr ? "is-na" : matchRateToneClass(ocrMatch.rate)
                    }`}
                    title={matchRateTitle(ocrMatch)}
                    aria-label={`OCR match rate ${formatMatchRatePercent(ocrMatch.rate)}`}
                  >
                    <span className="ocr-match-label">Match rate</span>
                    <span className="ocr-match-value">
                      {loadingOcr ? "…" : formatMatchRatePercent(ocrMatch.rate)}
                    </span>
                    {!loadingOcr && ocrMatch.count > 0 ? (
                      <span className="ocr-match-engines">
                        ({ocrMatch.count}{" "}
                        {ocrMatch.count === 1 ? "Method" : "Methods"})
                      </span>
                    ) : null}
                  </div>
                  <div
                    className={`ocr-quality-badge ${
                      pageQualityLabel
                        ? `is-${pageQualityLabel.toLowerCase()}`
                        : "is-na"
                    }`}
                    title={
                      pageQualityLabel
                        ? `Page quality from imaging analysis: ${pageQualityLabel}`
                        : "Page quality not available"
                    }
                    aria-label={`Quality ${pageQualityLabel ?? "NA"}`}
                  >
                    <span className="ocr-quality-label">Quality</span>
                    <span className="ocr-quality-value">
                      {loadingImaging && !pageQualityLabel
                        ? "…"
                        : pageQualityLabel ?? "NA"}
                    </span>
                  </div>
                  {showHeaderToggle ? (
                    <label className="ocr-section-headers-toggle">
                      <input
                        type="checkbox"
                        checked={showSectionHeaders}
                        onChange={(e) => setShowSectionHeaders(e.target.checked)}
                      />
                      Show Headers
                    </label>
                  ) : null}
                </div>
              )}
              {outputMode === "imaging" && (
                <div className="output-tabs" role="tablist" aria-label="Imaging views">
                  <button
                    type="button"
                    role="tab"
                    aria-selected={imagingTab === "page"}
                    className={imagingTab === "page" ? "active" : ""}
                    onClick={() => setImagingTab("page")}
                  >
                    Page Details
                  </button>
                  <button
                    type="button"
                    role="tab"
                    aria-selected={imagingTab === "doc"}
                    className={imagingTab === "doc" ? "active" : ""}
                    onClick={() => setImagingTab("doc")}
                  >
                    Doc Summary
                  </button>
                  <button
                    type="button"
                    role="tab"
                    aria-selected={imagingTab === "additional"}
                    className={imagingTab === "additional" ? "active" : ""}
                    onClick={() => setImagingTab("additional")}
                  >
                    Additional
                  </button>
                </div>
              )}
            </div>
            <div className="ocr-panel" role="tabpanel">
              {outputMode === "imaging" ? (
                <ImagingPanel
                  tab={imagingTab}
                  loading={
                    loadingFolder ||
                    loadingImaging ||
                    (!imagingDoc && !imagingError)
                  }
                  error={imagingError}
                  document={imagingDoc}
                  shellManifest={folder?.manifest ?? null}
                  currentPage={imagingPage}
                  currentFileName={page?.filename ?? null}
                  sectionHeaders={imagingSectionInfo.headers}
                  sectionHeadersSource={imagingSectionInfo.source}
                  sectionHeadersSkipped={imagingSectionInfo.skipped}
                  sectionHeadersLoading={loadingOcr}
                  imageNaturalSize={imageNaturalSize}
                />
              ) : loadingOcr ? (
                <div className="ocr-loading">Loading OCR output…</div>
              ) : pageOcrLines ? (
                <div className="ocr-formatted" aria-label="OCR text">
                  {pageOcrLines.map((line, i) => {
                    if (line.kind === "heading") {
                      return (
                        <div
                          key={i}
                          className={`ocr-line ocr-heading ocr-heading-h${Math.min(line.level, 3)}`}
                        >
                          {line.text}
                        </div>
                      );
                    }
                    return (
                      <div key={i} className="ocr-line ocr-text">
                        {line.text || "\u00a0"}
                      </div>
                    );
                  })}
                </div>
              ) : (
                <pre>{pageOcrText || "No OCR text for this page."}</pre>
              )}
            </div>
            {outputMode === "ocr" && (
              <div className="ocr-panel-footer">
                <button
                  type="button"
                  className="ocr-footer-btn"
                  disabled={loadingOcr || !canUseFull}
                  onClick={() => void copyFullOcr()}
                  title={`Copy full ${OCR_TAB_LABELS[ocrTab]} text`}
                >
                  <Copy size={14} aria-hidden="true" />
                  {copied ? "Copied" : "Copy"}
                </button>
                <button
                  type="button"
                  className="ocr-footer-btn primary"
                  disabled={loadingOcr || !canUseFull}
                  onClick={downloadFullOcr}
                  title={`Download full ${OCR_TAB_LABELS[ocrTab]} file`}
                >
                  <Download size={14} aria-hidden="true" />
                  Download
                </button>
              </div>
            )}
            {outputMode === "imaging" && (
              <div className="ocr-panel-footer">
                <button
                  type="button"
                  className="ocr-footer-btn"
                  disabled={loadingImaging || !imagingDoc}
                  onClick={downloadImagingDocCsv}
                  title="Download document imaging as CSV"
                >
                  <Download size={14} aria-hidden="true" />
                  Document CSV
                </button>
                <button
                  type="button"
                  className="ocr-footer-btn primary"
                  disabled={loadingImaging || !imagingDoc}
                  onClick={downloadImagingDocJson}
                  title="Download document imaging as JSON"
                >
                  <Download size={14} aria-hidden="true" />
                  Document JSON
                </button>
              </div>
            )}
          </section>
        </div>
      )}
    </div>
  );
}
