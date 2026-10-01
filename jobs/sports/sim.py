"""Monte Carlo draws for a slate from per-player projection percentiles, with player-to-player correlation.

Each player's score distribution is his quantile curve (SaberSim's p25/50/75/85/95/99 plus the mean), extended
with a lower and an upper tail anchor and sampled by inverse transform. Correlation comes from a Gaussian copula:
the uniforms are built from correlated normals whose correlation matrix is set by sport-specific rules
(CORR below). The rule values are the empirical correlations of standardized residuals (actual - projection)
measured on the archive itself (jobs/sports/corr_est.py):

  MLB   hitters of one team +.12 (adjacent batting-order spots +.15), hitter vs the opposing pitcher -.05
  NHL   same even-strength line F-F +.25 / D-F +.06 / D-D +.08, same team skaters +.03..+.05, G vs opposing skaters -.2 (F) / -.14 (D)
  TEN / MMA   opponents -.85 / -.80 (one wins)        LOL  teammates +.58, players vs the opposing TEAM slot -.5
  F1    teammates +.15                                NBA / WNBA / CBB / CFB  ~0 (measured -.01..+.03; not modelled)
  UFL / NFL   DST vs the opposing offense -.15; NFL QB with his own pass catchers +.3 (from the NFL engine)

sim_matrix(slate, n) -> (idx array of simulated players, draws[n_players, n]) ; players with proj 0 are skipped.
"""
from __future__ import annotations

import numpy as np

LOWER_P, UPPER_P = 0.02, 0.999


def _bucket(sport, p):
    pos = p["pos"]
    if sport == "MLB":
        return "P" if "P" in pos else "H"
    if sport == "NHL":
        return "G" if "G" in pos else ("D" if "D" in pos else "F")
    if sport in ("CFB", "UFL", "NFL"):
        for t in ("QB", "RB", "WR", "TE", "DST"):
            if t in pos:
                return t
        return "X"
    if sport == "LOL":
        return "TEAM" if "TEAM" in pos else "P"
    return "X"


def corr_matrix(sport: str, ps: list[dict]) -> np.ndarray:
    n = len(ps)
    R = np.eye(n, dtype=np.float64)
    if n < 2:
        return R
    team = np.array([p["team"] or f"_{p['base']}" for p in ps])
    opp = np.array([p["opp"] for p in ps])
    b = np.array([_bucket(sport, p) for p in ps])
    same_team = (team[:, None] == team[None, :]) & (team[:, None] != "")
    opp_m = (opp[:, None] == team[None, :]) & (opp[:, None] != "")
    opp_m = opp_m | opp_m.T
    if sport == "TEN" or sport == "MMA":
        # individual sports: "team" is the player, opp names the opponent
        name = np.array([p["name"] for p in ps])
        vs = np.array([p["opp"] for p in ps])
        m = (vs[:, None] == name[None, :]) & (vs[:, None] != "")
        R[m | m.T] = -0.85 if sport == "TEN" else -0.80
    elif sport == "MLB":
        H = b == "H"
        hh = same_team & H[:, None] & H[None, :]
        R[hh] = 0.12
        order = np.array([int(p["order"]) if p["order"].isdigit() else -9 for p in ps])
        adj = hh & (order[:, None] > 0) & (order[None, :] > 0) & (np.abs(order[:, None] - order[None, :]) % 9 <= 1) & (np.abs(order[:, None] - order[None, :]) % 9 > 0)
        adj |= hh & (order[:, None] > 0) & (order[None, :] > 0) & (np.abs(order[:, None] - order[None, :]) == 8)
        R[adj] = 0.15
        P = b == "P"
        R[opp_m & ((H[:, None] & P[None, :]) | (P[:, None] & H[None, :]))] = -0.05
    elif sport == "NHL":
        G = b == "G"; D = b == "D"; F = b == "F"
        sk = ~G
        R[same_team & sk[:, None] & sk[None, :]] = 0.03
        R[same_team & ((D[:, None] & F[None, :]) | (F[:, None] & D[None, :]))] = 0.05
        line = np.array([f"{p['team']}|{p['line']}" if p["line"] and p["line"] not in ("—", "-", "0") else f"_{i}" for i, p in enumerate(ps)])
        same_line = same_team & (line[:, None] == line[None, :]) & sk[:, None] & sk[None, :]
        R[same_line & F[:, None] & F[None, :]] = 0.25
        R[same_line & ((D[:, None] & F[None, :]) | (F[:, None] & D[None, :]))] = 0.06
        R[same_line & D[:, None] & D[None, :]] = 0.08
        R[same_team & ((G[:, None] & sk[None, :]) | (sk[:, None] & G[None, :]))] = 0.07
        R[opp_m & ((G[:, None] & F[None, :]) | (F[:, None] & G[None, :]))] = -0.20
        R[opp_m & ((G[:, None] & D[None, :]) | (D[:, None] & G[None, :]))] = -0.14
        R[opp_m & G[:, None] & G[None, :]] = -0.30
    elif sport == "LOL":
        P = b == "P"; T = b == "TEAM"
        R[same_team & P[:, None] & P[None, :]] = 0.58
        R[opp_m & ((P[:, None] & T[None, :]) | (T[:, None] & P[None, :]))] = -0.50
    elif sport == "F1":
        R[same_team] = 0.15          # teammates, and a constructor with its own drivers
    elif sport in ("UFL", "NFL"):
        DST = b == "DST"
        off = ~DST
        R[opp_m & ((DST[:, None] & off[None, :]) | (off[:, None] & DST[None, :]))] = -0.15
        if sport == "NFL":
            QB = b == "QB"; PC = (b == "WR") | (b == "TE")
            R[same_team & ((QB[:, None] & PC[None, :]) | (PC[:, None] & QB[None, :]))] = 0.30
    np.fill_diagonal(R, 1.0)
    # CPT and base rows of the same player are the same score: perfectly correlated
    base = np.array([p["base"] for p in ps])
    dup = (base[:, None] == base[None, :])
    R[dup] = 1.0
    return R


def _chol(R: np.ndarray) -> np.ndarray:
    try:
        return np.linalg.cholesky(R)
    except np.linalg.LinAlgError:
        w, V = np.linalg.eigh(R)
        w = np.clip(w, 1e-6, None)
        A = (V * np.sqrt(w)) @ V.T
        d = np.sqrt(np.diag(A))
        A = A / d[:, None] / d[None, :]
        return np.linalg.cholesky(A + 1e-8 * np.eye(len(A)))


def quantile_curve(p: dict) -> tuple[np.ndarray, np.ndarray]:
    """(probabilities, values) of the player's score distribution, monotone, with tail anchors."""
    q = p["q"]
    ps = [0.25, 0.50, 0.75, 0.85, 0.95, 0.99]
    vs = [q[25], q[50], q[75], q[85], q[95], q[99]]
    if max(vs) <= 0:
        vs = [p["proj"] * f for f in (0.6, 0.95, 1.3, 1.5, 1.85, 2.3)]
    lo = q[25] - max(q[50] - q[25], 0.25 * p["proj"])
    lo = min(lo, q[25])
    hi = vs[-1] + 0.5 * max(vs[-1] - vs[-2], 0.1 * p["proj"])
    probs = np.array([LOWER_P] + ps + [UPPER_P])
    vals = np.maximum.accumulate(np.array([lo] + vs + [hi], dtype=np.float64))
    return probs, vals


def sim_matrix(slate: dict, n: int = 10000, seed: int | None = None, players: list[dict] | None = None):
    """Correlated score draws for every projected player. Returns (players_simulated, draws[np, n], mean_shift)."""
    rng = np.random.default_rng(seed)
    ps = players if players is not None else [p for p in slate["players"] if p["proj"] > 0]
    if not ps:
        return ps, np.zeros((0, n), dtype=np.float32)
    R = corr_matrix(slate["sport"], ps)
    L = _chol(R)
    # one normal per BASE player so CPT rows reuse their driver's / player's draw exactly
    Z = L @ rng.standard_normal((len(ps), n))
    from scipy.stats import norm
    U = norm.cdf(Z)
    out = np.empty((len(ps), n), dtype=np.float32)
    for k, p in enumerate(ps):
        probs, vals = quantile_curve(p)
        x = np.interp(U[k], probs, vals, left=vals[0], right=vals[-1])
        shift = p["proj"] - x.mean()          # keep SaberSim's mean as the mean of the draws
        out[k] = np.maximum(x + shift, vals[0] + min(shift, 0.0))
    return ps, out
