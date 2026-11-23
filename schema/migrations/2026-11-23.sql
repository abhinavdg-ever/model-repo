-- 2026-11-23: page_continuity_results.decided_by allows the ML continuity mode's reasons,
-- 'model' and 'visual_continues' (CONTINUITY_MODE=ml). Without them the stage's insert failed
-- the CHECK and the chart had no documents (Encounter empty). Re-runnable.

ALTER TABLE page_continuity_results DROP CONSTRAINT IF EXISTS page_continuity_results_decided_by_check;
ALTER TABLE page_continuity_results ADD CONSTRAINT page_continuity_results_decided_by_check
    CHECK (decided_by IN (
        'chart_start','decisive','decisive_conflict','sum','tie',
        'conflict_a','conflict_b','final_pass','attached',
        'model','visual_continues',
        'first_page','blank_junk','same_dos','new_dos','pagination',
        'signature','page_number_end','weighted',
        'first_page_marker','page_type','page_end','continue_page','progress_note'
    )) NOT VALID;
