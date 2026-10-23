import { useState } from "react";
import { ArrowLeft, Play } from "lucide-react";
import PageMessage from "./PageMessage";
import { errorDetail, startPlaygroundRun, type AppConfig, type PlaygroundRunResponse } from "./api";

type Props = {
  config: AppConfig | null;
  onBack: () => void;
};

/** Charts one run may start (the server refuses more). */
const MAX_CHARTS = 10;

/** Chart names as the server reads them: split on commas, spaces and new lines,
 * quotes and brackets dropped, each once. */
export function chartNames(text: string): string[] {
  const out: string[] = [];
  for (const raw of text.split(/[\s,;]+/)) {
    const name = raw.trim().replace(/^["'`[\](){}]+|["'`[\](){}]+$/g, "").trim();
    if (name && !out.includes(name)) out.push(name);
  }
  return out;
}

/** As the server derives it: the last folder dropped, Raw_Input -> Processed
 * (Raw_Input/Run2/Batch6/DEID_PNGs -> Processed/Run2/Batch6). */
export function outputPathFor(inputPath: string): string {
  let parts = inputPath.split(/[\\/]+/).map((p) => p.trim()).filter(Boolean);
  if (parts.length > 1) parts = parts.slice(0, -1);
  if (parts[0]?.toLowerCase() === "raw_input") parts = parts.slice(1);
  return ["Processed", ...parts].join("/");
}

export default function PlaygroundView({ config, onBack }: Props) {
  const targets = config?.playground_targets ?? [];
  // VM first, by default; there is no blank choice.
  // VM by default (else the first); there is no blank choice.
  const defaultTarget = (targets.find((t) => t.label.toLowerCase() === "vm") ?? targets[0])?.url ?? "";
  const [target, setTarget] = useState(defaultTarget);
  const [container, setContainer] = useState(config?.playground_container || "imaging-pipeline");
  const [inputPath, setInputPath] = useState("");
  const [charts, setCharts] = useState("");
  const [skipOcr, setSkipOcr] = useState(true);
  const [skipCompleted, setSkipCompleted] = useState(false);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<PlaygroundRunResponse | null>(null);

  const enabled = Boolean(config?.playground_enabled);
  const outputPath = inputPath.trim() ? outputPathFor(inputPath) : "";
  const names = chartNames(charts);
  const chartProblem = !names.length
    ? "List at least one chart"
    : names.length > MAX_CHARTS
      ? `${names.length} charts listed: at most ${MAX_CHARTS} per run`
      : null;
  const chosenTarget = targets.some((t) => t.url === target) ? target : defaultTarget;

  async function run() {
    setRunning(true);
    setError(null);
    setResult(null);
    try {
      setResult(
        await startPlaygroundRun({
          target: chosenTarget,
          container_name: container.trim(),
          input_path: inputPath.trim(),
          chart_list: names.join(","),
          skip_ocr: skipOcr,
          skip_completed: skipCompleted,
        }),
      );
    } catch (err) {
      setError(errorDetail(err, "Could not start the run"));
    } finally {
      setRunning(false);
    }
  }

  return (
    <div className="workspace accuracy-view playground-view">
      <div className="workspace-header">
        <div className="workspace-header-start">
          <button type="button" className="back-btn" onClick={onBack}>
            <ArrowLeft size={15} aria-hidden="true" />
            Back
          </button>
          <div className="workspace-title">
            <h1>Playground</h1>
          </div>
        </div>
      </div>

      <section className="accuracy-section" aria-label="Batch run">
        <h2>Batch Run</h2>
        <p className="accuracy-note">
          Starts a core-pipeline batch run over the charts listed (up to {MAX_CHARTS}) in a blob
          folder. Results are written to the matching Processed folder.
        </p>
        {!enabled ? (
          <PageMessage kind="error">
            The Playground is off on this server: set CORE_PIPELINE_URLS in review-ui/.env.
          </PageMessage>
        ) : null}

        <div className="playground-form">
          <label>
            <span>Pipeline</span>
            <select value={chosenTarget} required onChange={(e) => setTarget(e.target.value)}>
              {targets.map((t) => (
                <option key={t.url} value={t.url}>
                  {t.label} ({t.url})
                </option>
              ))}
            </select>
            {!targets.length ? <small>No pipeline configured</small> : null}
          </label>
          <label>
            <span>Container</span>
            <input value={container} onChange={(e) => setContainer(e.target.value)} />
          </label>
          <label>
            <span>Input blob path</span>
            <input
              value={inputPath}
              placeholder="Raw_Input/Run2/Batch6/DEID_PNGs"
              onChange={(e) => setInputPath(e.target.value)}
            />
          </label>
          <label>
            <span>Output path</span>
            <input value={outputPath} readOnly placeholder="Processed/Run2/Batch6" />
            <small>Derived: the last folder dropped, Raw_Input becomes Processed.</small>
          </label>
          <label>
            <span>Charts (required, up to {MAX_CHARTS})</span>
            <textarea
              rows={3}
              value={charts}
              required
              placeholder="52743839_44976074, 52754737_48221214 — one per line or comma-separated; no quotes needed"
              onChange={(e) => setCharts(e.target.value)}
            />
            <small className={charts.trim() && chartProblem ? "playground-error" : undefined}>
              {charts.trim() && chartProblem ? chartProblem : `${names.length} of ${MAX_CHARTS}`}
            </small>
          </label>
          <div className="playground-checks">
            <label>
              <input type="checkbox" checked={skipOcr} onChange={(e) => setSkipOcr(e.target.checked)} />
              Skip OCR (reuse existing OCR)
            </label>
            <label>
              <input
                type="checkbox"
                checked={skipCompleted}
                onChange={(e) => setSkipCompleted(e.target.checked)}
              />
              Skip completed charts
            </label>
          </div>
          <div>
            <button
              type="button"
              className="landing-file-viewer-btn"
              disabled={
                !enabled || running || !chosenTarget || !inputPath.trim() || !container.trim() || Boolean(chartProblem)
              }
              onClick={run}
            >
              <Play size={14} aria-hidden="true" />
              {running ? "Starting…" : "Start run"}
            </button>
          </div>
        </div>

        {error ? (
          <PageMessage kind="error" onClose={() => setError(null)}>
            Could not start the run: {error}
          </PageMessage>
        ) : null}
        {result ? (
          <PageMessage kind="success" half onClose={() => setResult(null)}>
            Job submitted successfully. Please view History when it finishes.
          </PageMessage>
        ) : null}
      </section>
    </div>
  );
}
