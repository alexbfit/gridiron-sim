"""DraftKings Classic roster rules per sport.

Derived from the real contest fields in DFS DATA/_sabersim_allsports (every slot of every lineup checked
against the players' DK position strings) and cross-checked with DraftKings' published rules.

A player's `pos` string from SaberSim lists every slot he is eligible for ("PG/G/UTIL", "RB/FLEX/S-FLEX",
"C/1B"); SLOTS gives, per roster slot, the tokens that make a player eligible. Sports without positions
(golf, NASCAR, tennis, MMA) use a single "X" token that every player carries. Captain formats (F1, LoL)
have separate CPT rows in the archive (uuid ends in "CPT", 1.5x points, higher salary) - the CPT slot takes
only those rows, every other slot only the base rows, and a base/CPT pair of the same player can't coexist.
"""
from __future__ import annotations

CAP = 50000

RULES = {
    "NBA":    {"slots": [("PG", {"PG"}), ("SG", {"SG"}), ("SF", {"SF"}), ("PF", {"PF"}), ("C", {"C"}),
                         ("G", {"G"}), ("F", {"F"}), ("UTIL", {"UTIL"})],
               "min_games": 2, "max_team": 8},
    "WNBA":   {"slots": [("G", {"G"}), ("G", {"G"}), ("F", {"F"}), ("F", {"F"}), ("F", {"F"}), ("UTIL", {"UTIL"})],
               "min_games": 2, "max_team": 6},
    "CBB":    {"slots": [("G", {"G"}), ("G", {"G"}), ("G", {"G"}), ("F", {"F"}), ("F", {"F"}), ("F", {"F"}),
                         ("UTIL", {"UTIL"}), ("UTIL", {"UTIL"})],
               "min_games": 2, "max_team": 8},
    "NHL":    {"slots": [("C", {"C"}), ("C", {"C"}), ("W", {"W"}), ("W", {"W"}), ("W", {"W"}), ("D", {"D"}), ("D", {"D"}),
                         ("G", {"G"}), ("UTIL", {"UTIL"})],
               "min_games": 2, "max_team": 8},
    "MLB":    {"slots": [("P", {"P"}), ("P", {"P"}), ("C", {"C"}), ("1B", {"1B"}), ("2B", {"2B"}), ("3B", {"3B"}),
                         ("SS", {"SS"}), ("OF", {"OF"}), ("OF", {"OF"}), ("OF", {"OF"})],
               "min_games": 2, "max_team": 10, "max_hitters_team": 5},      # DK: max 5 hitters from one team
    "SOCCER": {"slots": [("F", {"F"}), ("F", {"F"}), ("M", {"M"}), ("M", {"M"}), ("D", {"D"}), ("D", {"D"}),
                         ("GK", {"GK"}), ("UTIL", {"UTIL"})],
               "min_games": 2, "max_team": 8},
    "CFB":    {"slots": [("QB", {"QB"}), ("RB", {"RB"}), ("RB", {"RB"}), ("WR", {"WR"}), ("WR", {"WR"}), ("WR", {"WR"}),
                         ("FLEX", {"FLEX"}), ("S-FLEX", {"S-FLEX"})],
               "min_games": 2, "max_team": 8},
    "UFL":    {"slots": [("QB", {"QB"}), ("RB", {"RB"}), ("WR/TE", {"WR", "TE"}), ("WR/TE", {"WR", "TE"}),
                         ("FLEX", {"FLEX"}), ("FLEX", {"FLEX"}), ("DST", {"DST"})],
               "min_games": 2, "max_team": 7},
    "GOLF":   {"slots": [("G", {"X"})] * 6, "min_games": 1, "max_team": 6},
    "NASCAR": {"slots": [("D", {"X"})] * 6, "min_games": 1, "max_team": 6},
    "TEN":    {"slots": [("P", {"X"})] * 6, "min_games": 1, "max_team": 6},
    "MMA":    {"slots": [("F", {"X"})] * 6, "min_games": 1, "max_team": 6},
    "F1":     {"slots": [("CPT", {"CPT"})] + [("D", {"X"})] * 4 + [("CNSTR", {"CNSTR"})], "min_games": 1, "max_team": 3},
    "LOL":    {"slots": [("CPT", {"CPT"}), ("TOP", {"TOP"}), ("JNG", {"JNG"}), ("MID", {"MID"}), ("ADC", {"ADC"}),
                         ("SUP", {"SUP"}), ("TEAM", {"TEAM"})],
               "min_games": 2, "max_team": 4},
}

# sports whose rows carry no position string: every player is eligible for every slot
NO_POSITIONS = {"GOLF", "NASCAR", "TEN", "MMA", "F1"}

# which sports count "games" for the 2-games rule by team/opp pairs (individual sports have none)
TEAM_SPORTS = {"NBA", "WNBA", "CBB", "NHL", "MLB", "SOCCER", "CFB", "UFL", "LOL"}

# stack definitions: (group key, positions that count) -> `--stack k` means k players sharing the key
STACK_KEYS = {
    "MLB": "team_hitters",      # hitters from one team (pitchers never count)
    "NHL": "line",              # skaters on the same even-strength line (team + linePosition)
    "NBA": "game",              # players from one game
    "WNBA": "game",
    "CBB": "game",
    "CFB": "team_pass",         # QB + pass catchers of one team
    "UFL": "team_pass",
    "SOCCER": "team",
    "LOL": "team",
}


def roster_size(sport: str) -> int:
    return len(RULES[sport]["slots"])


def eligible(sport: str, pos_tokens: set[str], slot_tokens: set[str]) -> bool:
    return bool(pos_tokens & slot_tokens)
