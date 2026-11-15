-- 2026-11-15: chart_list.chart_cluster, the chart's segment from chart-clustering (C01, C02 ...
-- or Others). Written by `python -m chart_clustering ... --write-db`; review-ui filters on it.
-- Re-runnable.

ALTER TABLE chart_list ADD COLUMN IF NOT EXISTS chart_cluster VARCHAR(100);
