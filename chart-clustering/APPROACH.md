# Chart Clustering: first cut, approach

Date: 2026-11-15. Basis: *Chart Clustering: Concept Note* (6 Nov 2026). This folder is a
**first cut** of that note: a fully **unsupervised** segmentation of a set of charts (about 550)
from one or two **anchor pages** per chart, instead of every Progress Note page. It is
standalone code (nothing imported from core-pipeline) and reads charts from the **database**,
**Azure Blob**, or a **local folder**.

## 1. Why anchor pages first

The concept note types a chart by how its **progress notes** look. Building the full
fingerprint (a 32-look codebook over every note page, DINOv2 images, Docling layout) is the
end state. A first cut needs only a cheap, faithful signal:

- The **first page of the first Progress Note** carries the note template: the EMR header,
  the patient banner layout, the section order (Chief Complaint / HPI / ROS …) and the field
  labels. Charts from the same source and EMR share it.
- The **last page** carries the closing template: signature block, EMR footer, fax trailer,
  print stamp ("Printed by … Page x of y"), which is often the strongest sign of how the chart
  was retrieved.

Using both (`--anchors both`, the default) describes a chart by its note template and its
retrieval trail. Either one alone is available (`first_pn`, `last`).

## 2. Finding the anchor pages

| Anchor | With the pipeline's labels (DB `imaging_final`, or `imaging/<chart>_final.csv`) | Without labels (text only) |
|---|---|---|
| Progress Note pages (`--notes 3`) | the first page of each of the chart's first **2-3 Progress Notes** (a new note = a new continuity document, `document_seq`) | first kept page with 2+ note markers: Chief Complaint, HPI, ROS, Physical Exam, Assessment, Plan, Subjective, Objective, Vitals, Reason for Visit, Impression; a new note = a note page right after a page that is not one |
| Last page | last kept page with text | same |
| Fallback | first page with text | same |

"Kept" = not blank, junk or a duplicate (when labels exist). A page needs 15+ words to be
used. How each anchor was found is recorded per chart (`first_pn_how`, `last_how`).

The text is always **Tesseract** (stage 2): every chart has it on every page, so training
and later matching see the same text.

The words of the 2-3 note pages are pooled into one bag (2026-11-15, user: more than one
note per chart, so one odd note does not decide the chart). A chart with fewer notes uses what it has.

**What is downloaded:** text only. No page images (jpg) are needed for this first cut. `db` reads
the Tesseract text from the database (nothing downloaded to disk); `blob` reads two small files
per chart into memory (`ocr/<chart>_prelim.txt`, `imaging/<chart>_final.csv`).

## 3. Features (numbers only)

**Template words.** Each anchor page's words, lower-case, 3+ letters, no digits, plus each
line-start field label as one token (`Chief Complaint:`, `Appt. Date/Time:`). Then the
unsupervised filter that removes patient data: a word must be on **at least 3 charts**
(`--min-charts`) and on no more than 95% of them. Names, dates, ids and one-off values are on
one chart and drop out; template words (section names, labels, EMR and practice footers) are
on many and stay. Weighted with TF-IDF (sublinear), one vocabulary per anchor.

**Layout numbers** of the main anchor page and the chart: lines, words, share of "Label:"
lines (form-like vs narrative), upper-case line share, digit share, a fax header in the top
lines, average line length, chart page count, handwritten share and Progress Note share (with
labels). Log-scaled and standardised.

## 4. The algorithm (unsupervised)

1. **LSA:** TF-IDF → TruncatedSVD to ≤ 50 dimensions → unit length. Text of the same template
   lands close together whatever the patient content.
2. **Combine:** append the layout numbers × 0.35 (`--layout-weight`); text dominates.
3. **Cluster: HDBSCAN** (`--min-cluster-size 5`, `--min-samples 3`). It needs no number of
   clusters, finds groups of any shape and density, and leaves charts that fit no group as
   noise, which is exactly the concept note's "never force a chart into the nearest type".
4. **Others.** HDBSCAN's noise, plus charts with no usable anchor page (no text, under 15
   words), form **one cluster: Others** (2026-11-15, user).
5. **Names.** Clusters are numbered C01, C02 … by size. A person names them from the cards.

Fixed seed (42); the same input gives the same clusters.

## 5. Checking it without labels

| Check | How | Target |
|---|---|---|
| Separation | silhouette score on the clustered charts (`run_manifest.json`) | > 0.2 to start; higher is better |
| Stable | mean adjusted Rand index of 5 re-clusterings on 80% samples vs the full run | ≥ 0.8 (concept note) |
| Real | a person reads each card and 5-10 example charts | ≥ 90% look like one kind of chart |
| Others share | `run_manifest.json` clusters | watch it: a large Others means min-cluster-size is too high or the anchors are poor |

Tuning order: `--anchors` (both / first_pn / last) → `--min-cluster-size` (3–10) →
`--min-charts` → `--layout-weight`.

## 6. Outputs (`--out`)

| File | Holds |
|---|---|
| `chart_clusters.csv` | per chart: cluster, membership strength, anchor pages and how found, pages, kept pages, handwritten / Progress Note share |
| `cluster_summary.csv` | per cluster: size, share, top template words, layout averages, 10 example chart names |
| `cluster_cards.md` | the same, with a blank "Name:" line per cluster for the reviewer |
| `run_manifest.json` | settings, counts, silhouette, stability, data hash, version |
| `features.npz` | the clustering matrix and chart names (plots, re-use) |

**No page text, names, DOBs or member ids** in any output: the cards show only words on a
fixed list of generic document words (`chart_clustering/vocab.py`: sections, field labels,
EMR names). Chart names are ids.

## 7. Sources

| `--source` | Reads | Needs (in `.env`, never on the command line) |
|---|---|---|
| `db` | `chart_list` → `page_list` → `ocr_results` (`ocr_type = 'tesseract'`) + `imaging_final` labels | `DATABASE_URL` (`--schema`, default public) |
| `blob` | `<container>/<prefix>/<chart>/ocr/<chart>_prelim.txt` (+ `imaging/<chart>_final.csv`): the chart output folders core-pipeline exports | `AZURE_STORAGE_CONNECTION_STRING`, or `AZURE_STORAGE_ACCOUNT_NAME` + `_KEY`, or the account name with a managed identity / `az login` |
| `folder` | the same files under a local folder (`core-pipeline/outputs/charts` shape) | nothing |

`--chart-list charts.txt` (one name per line) limits any source to those charts, from any runs
/ batches: the database looks them up by name; Blob fetches each from its own `chart_list.output_path`
(`--paths-from-db`) or the first of several `--prefix` folders that has it. Charts not found are
listed in `missing_charts.txt`.

## 8. From first cut to the concept note (next steps, for Cursor)

| Step | Concept note | What to add here |
|---|---|---|
| 1 | C0 note pages | use **every** Progress Note page, not just the first: one vector per page, then per chart the mean and spread |
| 2 | C1 codebook | k-means (~32) over the note-page vectors = note looks; chart fingerprint = share of its note pages per look (Jensen-Shannon distance) |
| 3 | images | DINOv2-small on the cleaned page images (Blob pages) + Docling layout grid, when nearly all charts have images |
| 4 | C2 fingerprint | add the parts and weights: note-look mix 0.40, note overall look 0.15, specialty 0.15, capture 0.15, rest of chart 0.05, source 0.05, retrieval method 0.05 (Gower for source / method from the manifest) |
| 5 | C2 catalogue | per cluster: up to 10 prototypes and a distance limit (95th percentile of members' distance to their nearest prototype, 0.15–0.45) |
| 6 | C3 matcher | known / unsure / new with the limit and the 0.05 margin; `no_notes` (< 2 Progress Note pages), `new_notes` (30%+ new note looks) |
| 7 | C4 weekly retrain | group held charts with the same HDBSCAN; candidate = 3+ charts, 2+ deliveries, tight, far from known types, not just capture |
| 8 | C5 checks | "hide one type" test; drift report per client / retrieval channel |
| 9 | C6 pipeline | a shadow stage after imaging_final writing `chart_type` rows |

Open questions from the concept note still stand: manifest field names for client / project
and retrieval channel, the 3-charts-from-2-deliveries bar, the weekly reviewer, the full
retrain trigger.
