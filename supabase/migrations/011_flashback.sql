-- gridiron-sim: Contest Flashback (run after 010).
-- jobs/flashback.py re-scores the recorded lineups against the REAL contest field (every entry's lineup from the
-- DK standings export) across the stored sims: expected ROI under our projections ("model") and under market
-- projections ("cons" = construction only), plus the realized rank/prize. Summary per (source/contest) in slates.flashback.

alter table slate_lineups
  add column if not exists fb_roi       numeric,      -- expected profit / fee, our projections
  add column if not exists fb_cash      numeric,      -- P(any prize), our projections
  add column if not exists fb_top1      numeric,      -- P(top 1% of the field), our projections
  add column if not exists fb_roi_cons  numeric,      -- same three under market (props / salary-implied) projections
  add column if not exists fb_cash_cons numeric,
  add column if not exists fb_top1_cons numeric,
  add column if not exists real_rank    int,          -- where the actual total finished among the real entries
  add column if not exists real_prize   numeric,      -- prize at that rank on the assumed curve (split by exact duplicates)
  add column if not exists fb_at        timestamptz;

alter table slates add column if not exists flashback jsonb;   -- {"claude/gpp": {model: {...}, consensus: {...}, real_roi, ...}}
