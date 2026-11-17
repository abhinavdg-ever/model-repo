# Chart Clustering: Methodology

Version 0.2.0, 2026-11-17. Basis: *Chart Clustering: Concept Note* (6 Nov 2026), the text-only
version (2026-11-15) and the teammate handoff (2026-11-17). How to run it: [README.md](README.md).

---

## 1. Purpose and scope

Group a set of charts (about 550) into **chart types** without any labels. A chart type is a
family of charts that were produced and retrieved the same way: the same EMR or note template,
the same practice, the same capture trail (fax, portal print, scan). The groups are named
`C01`, `C02` … by size, plus one **Others** group for charts that fit none. A person then names
each group from its card.

This is fully **unsupervised**. Nothing is trained on client labels, and nothing is forced: a
chart that does not resemble any group goes to Others rather than to the nearest group (the
concept note's rule).

The package is standalone. It imports nothing from core-pipeline, reads what the pipeline
already wrote (database or Blob), and changes nothing except, on request, one column
(`chart_list.chart_cluster`) that review-ui filters on.

## 2. What a chart is described by

A chart is described by a few **anchor pages**, not every page. They carry the template
cheaply and faithfully.

| Anchor | Why it is informative | How it is found |
|---|---|---|
| **First pages of up to 3 Progress Notes** (`--notes 3`) | The note template: EMR header, patient banner layout, section order (Chief Complaint, HPI, ROS …), field labels. Charts from the same source and EMR share it. | With the pipeline's labels (`imaging_final` / `imaging/<chart>_final.csv`): the first page of each of the first 3 continuity documents typed Progress Note. Without labels: a page with 2+ note markers, and a new note starts after a page that is not one. |
| **Last page** | The closing template: signature block, EMR footer, fax trailer, print stamp ("Printed by … Page x of y"), often the clearest sign of the retrieval channel. | The last kept page with text. |
| Fallback | So no chart is silently dropped | The first page with text. |

"Kept" means not blank, junk or a duplicate (when labels exist). A page needs 15+ words to
count. Pooling 2–3 notes means one odd note cannot decide the chart. How each anchor was
found is recorded per chart (`first_pn_how`, `last_how`).

## 3. Two signals

### 3.1 Text (template words + layout)

Source: **Tesseract** text (pipeline stage 2), which every chart has on every page, so the
same text is used for building and for matching later.

1. **Template words.** The anchor pages' words (lower case, 3+ letters, no digits) plus each
   line-start field label as one token (`label:chief complaint`). The vocabulary filter is
   what removes patient data, without a list of names: a word must be on **at least 3 charts**
   (`--min-charts`) and on no more than 95% of them. Names, dates, ids and one-off values are on
   one chart and drop out. Section names, labels, EMR and practice footers are on many and stay.
2. **TF-IDF** (sublinear), one vocabulary per anchor (notes, last page), stacked.
3. **LSA:** TruncatedSVD to at most 50 dimensions, then unit length per chart. Text of the same
   template lands close together whatever the patient content.
4. **Layout numbers** of the main anchor page and the chart: lines, words, share of "Label:"
   lines (form vs narrative), upper-case line share, digit share, a fax header in the top
   lines, average line length, chart page count, handwritten share, Progress Note share.
   Log-scaled, standardised, weighted 0.35 (`--layout-weight`) and appended.

### 3.2 Visual (page-image fingerprint)

Source: the **original page images** in Azure Blob **Raw_Input**, from each chart's
`chart_list.blob_path` (e.g. `Raw_Input/Run1/Batch1/DEID_PNGs/<chart>/1.jpg`). Only the same
anchor pages are used (the Progress Note first pages, else the fallback page), so the visual
and text signals describe the same pages.

Each page image is turned grey, set upright (EXIF), resized to a fixed 128 × 160 portrait grid,
and measured (dark = 1, white = 0):

| Part | Size | What it captures |
|---|---|---|
| Thumbnail | 16 × 20 cells of mean darkness | where the page is dark: logo, header band, banner, tables |
| Ink rows | 40 horizontal bands, ink share | line spacing, header and footer blocks |
| Ink columns | 32 vertical bands, ink share | one column, two columns, a left label column |
| Edges | 8 × 10 cells of edge density | text blocks vs ruled forms and boxes |
| Page | 3 numbers | ink share, darkness spread, aspect ratio |

A chart's vector is the mean over its anchor pages (535 numbers). Across charts the vectors
are standardised, reduced with **PCA** to at most 50 dimensions, and set to unit length.

This is a classical, CPU-only fingerprint: no model download, deterministic, fast. The planned
upgrade is a self-supervised image embedding (DINOv2-small) in the same slot (§9).

Images are downloaded once into `--image-cache/<chart>/<page>` and reused by later runs. Where
an image is looked for, in order: `{blob_path}/{page name}`, then a listing of that Raw_Input
folder (sub-folders too) for the same file name, then, only with `--processed-fallback`,
`{output_path}/<chart>/pages/{page name}`. A page that cannot be downloaded or read is
skipped with a warning; one bad page never stops the run.

## 4. Tracks: which signal is clustered

| `--track` | Matrix clustered | Charts that can be grouped |
|---|---|---|
| `text` | text block (§3.1) | charts with a usable text anchor |
| `visual` | visual block (§3.2) | charts with at least one readable image |
| **`mix`** (default) | both, fused: each block set to unit length per chart, the visual block multiplied by `--visual-weight` (0.5), the two placed side by side | charts with a usable text anchor; a chart with no image keeps a zero visual block |
| `both` | `text` and `visual` run separately, into `out/text` and `out/visual` | as above, per track |

The 0.5 weight lets the note template (text) lead while the page look separates charts whose
words agree but whose capture differs (a fax copy vs a portal print of the same EMR). If no
chart has an image, `mix` clusters exactly as `text` does, and the manifest says so
(`visual_used: false`). Nothing degrades silently.

## 5. Grouping: HDBSCAN

The chosen matrix is grouped with **HDBSCAN** (`--min-cluster-size 5`, `--min-samples 3`). It
needs no number of groups (the data decides), finds groups of any shape and density, and
leaves charts that fit no group as noise. That noise, plus charts with no usable anchor,
becomes **Others**: a chart is never forced into the nearest group.

**What `min_cluster_size` means.** It is the fewest charts a group may have, not the number of
groups. Do not lower it to get more groups: that once produced 121 micro-clusters.

Groups are numbered C01, C02 … by size (largest first). A fixed seed (42) gives the same
groups for the same input. Numbers are per run: a new run can renumber them.

## 6. Checking quality without labels

| Check | How | Target |
|---|---|---|
| Separation | silhouette score on the grouped charts (`run_manifest.json`) | above 0.2 to start; higher is better |
| Stability | mean adjusted Rand index of 5 re-groupings on 80% samples against the full run | 0.8 or more (concept note); HDBSCAN does not always reach it |
| Real | a person reads each card and 5–10 example charts | 90% or more look like one kind of chart |
| Others share | the cluster sizes in `run_manifest.json` | watch it: a large Others means the minimum group size is too high, the anchors are poor, or images are missing |

Tuning order: `--track` (mix / text / visual) → `--visual-weight` → `--anchors` →
`--min-cluster-size` (3–10) → `--min-charts` → `--layout-weight`. `--track both` writes text
and visual side by side, so a choice is made from two results, not from memory.

## 7. Outputs

| File | Holds |
|---|---|
| `chart_clusters.csv` | per chart: cluster, membership strength, anchor pages and how found, pages, kept pages, handwritten and Progress Note shares, images used |
| `cluster_summary.csv` | per cluster: size, share, top template words, layout averages, 10 example chart names |
| `cluster_cards.md` | the same, readable, with a blank "Name:" line per cluster |
| `run_manifest.json` | track, every setting, cluster sizes, silhouette, stability, images found, data hash, version |
| `features.npz` | the matrix that was clustered and the chart names (plots, re-use) |
| `missing_charts.txt` | listed charts not found in the source |

With `--write-db` the labels also go to `chart_list.chart_cluster` (review-ui's Cluster
filter). With `--track both` the text labels are written.

## 8. Privacy

No page text, names, DOBs or member ids reach any output or log.

- Features are numbers.
- Cards show only words on a fixed list of generic document words (`vocab.py`: sections,
  field labels, EMR names).
- Chart names are ids.
- Page images stay in the local image cache, which is git-ignored and is the operator's to
  delete.
- Credentials come from `.env`, never from the command line, and are never printed.

## 9. Roadmap to the concept note

| Step | Concept note | To add |
|---|---|---|
| 1 | Images | DINOv2-small embeddings of the cleaned page images in place of the classical fingerprint (same slot, same weight) |
| 2 | C0 note pages | every Progress Note page, not only the first 3: one vector per page, per chart the mean and spread |
| 3 | C1 codebook | k-means (~32) over note-page vectors = note looks; chart fingerprint = share of its note pages per look (Jensen-Shannon distance) |
| 4 | C2 fingerprint | the weighted parts: note-look mix 0.40, note overall look 0.15, specialty 0.15, capture 0.15, rest of chart 0.05, source 0.05, retrieval method 0.05 |
| 5 | C2 catalogue | per cluster up to 10 prototypes and a distance limit (95th percentile, 0.15–0.45) |
| 6 | C3 matcher | known / unsure / new; `no_notes`, `new_notes` |
| 7 | C4 weekly retrain | group held charts with the same HDBSCAN; candidate = 3+ charts from 2+ deliveries |
| 8 | C5 checks | "hide one type" test; drift report per client and retrieval channel |
| 9 | C6 pipeline | a shadow stage after imaging_final writing chart-type rows |

Open questions from the concept note still stand: the manifest fields for client / project
and retrieval channel, the 3-charts-from-2-deliveries bar, the weekly reviewer, and the full
retrain trigger.
