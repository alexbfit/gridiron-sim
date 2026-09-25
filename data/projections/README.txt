Drop third-party projection CSVs here (any file with a player name + projection column),
named with the slate key, e.g. DK-2026-02-main_4for4.csv. Push, and the builder gets a "Blend external %" control.

SaberSim (adopted 2026-09-25): save SaberSim's projections for the DK main slate as DK-<season>-<wk>-main_sabersim.csv
(SaberSim -> Projections -> "Download player projections", or any CSV with Name/Team/Pos + "SS Proj"). The Sunday build and
late swaps pass --ext-file <that file> --ext-sim 1.0 (see data/contests/sabersim_2024_2026.md). Pushing it also imports it
as 'external' projections for the web builder.
