Extra contest standings exports (DK "Export lineups" CSV) that are NOT the slate's canonical ownership.

data/ownership/  = ONE file per slate: the big-field GPP we build for (Milly Maker). The pipeline imports it as the
                   slate's ownership + field distribution, and the ownership model is fitted to it.
data/contests/   = every other contest entered that week, kept for analysis (sharper fields, 20-max, cash).
                   Not auto-imported. Name: <slate-key>_<type>_<contestId>.csv
