import { useState } from "react";
import { ArrowLeft, Play } from "lucide-react";
import { errorDetail, startPlaygroundRun, type AppConfig, type PlaygroundRunResponse } from "./api";

type Props = {
  config: AppConfig | null;
  onBack: () => void;
};

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
  const [target, setTarget] = useState(targets[0] ?? "");
  const [container, setContainer] = useState(config?.playground_container || "imaging-pipeline");
  const [inputPath, setInputPath] = useState("");
  const [charts, setCharts] = useState("");
  const [sample, setSample] = useState("");
  const [skipOcr, setSkipOcr] = useState(false);
  const [skipCompleted, setSkipCompleted] = useState(false);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<PlaygroundRunResponse | null>(null);

  const enabled = Boolean(config?.playground_enabled);
  const outputPath = inputPath.trim() ? outputPathFor(inputPath) : "";

  async function run() {
    setRunning(true);
    setError(null);
    setResult(null);
    try {
      const count = Number.parseInt(sample, 10);
      setResult(
        await startPlaygroundRun({
          target: target || targets[0] || "",
          container_name: container.trim(),
          input_path: inputPath.trim(),
          chart_list: charts,
          sample: Number.isFinite(count) && count > 0 ? count : null,
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
        <h2>Run a batch of charts</h2>
        <p className="accuracy-note">
          Starts a core-pipeline batch run over every chart folder in a blob folder (or just the
          charts listed). Results are written to the matching Processed folder. Every stage
          reprocesses; Final OCR 2 is billed per page unless Skip OCR is on.
        </p>
        {!enabled ? (
          <p className="playground-error" role="alert">
            The Playground is off on this server: set CORE_PIPELINE_URLS in review-ui/.env.
          </p>
        ) : null}

        <div className="playground-form">
          <label>
            <span>Pipeline</span>
            <select value={target || targets[0] || ""} onChange={(e) => setTarget(e.target.value)}>
              {targets.map((url) => (
                <option key={url} value={url}>
                  {url}
                </option>
              ))}
            </select>
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
            <span>Charts (optional)</span>
            <textarea
              rows={3}
              value={charts}
              placeholder="Blank = every chart in the folder. One per line, or comma-separated."
              onChange={(e) => setCharts(e.target.value)}
            />
          </label>
          <label>
            <span>At most (optional)</span>
            <input
              type="number"
              min={1}
              value={sample}
              placeholder="All"
              onChange={(e) => setSample(e.target.value)}
            />
          </label>
          <div className="playground-checks">
            <label>
              <input type="checkbox" checked={skipOcr} onChange={(e) => setSkipOcr(e.target.checked)} />
              Skip OCR (reuse existing OCR, no Final OCR 2 bill)
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
              disabled={!enabled || running || !inputPath.trim() || !container.trim()}
              onClick={run}
            >
              <Play size={14} aria-hidden="true" />
              {running ? "Starting…" : "Start run"}
            </button>
          </div>
        </div>

        {error ? (
          <p className="playground-error" role="alert">
            {error}
          </p>
        ) : null}
        {result ? (
          <div className="playground-result" role="status">
            <p>
              Started on <code>{result.target}</code>: <code>{result.request.input_path}</code> → <code>{result.request.output_path}</code>.
              It runs in the background; the charts appear in History as they finish.
            </p>
            <pre>{JSON.stringify(result.pipeline, null, 2)}</pre>
          </div>
        ) : null}
      </section>
    </div>
  );
}
