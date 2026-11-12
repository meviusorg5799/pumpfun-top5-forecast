#!/usr/bin/env python3
"""Forecast the top 5 pump.fun coins by market cap, 24 hours out.

Reads /input/history.ndjson (10-minute snapshot marks) and /input/task.json,
writes /output/forecast.json (probabilities, top5, sizes).

Method (stdlib only, deterministic, offline):
  * Every coin seen in the history gets a log10 market-cap (in SOL) model:
    a drift estimated from its recent slope, shrunk toward the pooled
    age-bucket drift, plus a volatility that grows with the forecast horizon.
  * Newcomer "ghost" coins model coins launched after the history ends:
    their arrival rate (coins reaching top-5-level caps within an hour of
    launch) and their cap distribution are estimated from the history
    itself, so the forecast leaves honest unnamed mass for coins it cannot
    name. In this market the top 5 turns over in minutes: most of the
    winners at the answer reads are coins nobody's history can name.
  * A fixed-seed Monte Carlo over the seven answer reads ranks the known
    coins against the ghosts; place probabilities and size-bin
    probabilities are the Monte Carlo frequencies averaged over the reads.
  * A coin is named at a place only when its probability beats the
    per-coin unnamed share there; coins the simulation never puts in the
    top 5 and that are already far down the chart are named at p=0, which
    concentrates the unnamed mass on the coins that can actually win.

Times are read from task.json, never hard-coded. Two runs on the same
history write the same file byte for byte (fixed seed, sorted iteration).
"""

import heapq
import json
import math
import os
import random
import sys
from datetime import datetime, timezone

SEED = 0x5EEDF00D
NSIM = 3000
LAMPORTS_PER_SOL = 1e9
# Extra dispersion (log10 SOL) convolved into each place's size
# distribution. The Monte Carlo's place caps are too narrow on their own:
# the realised caps at the reads move further than any single coin's
# trajectory suggests (selection over thousands of coins, ramps), so the
# size bins need a wider spread than the raw simulation gives.
SIZE_WIDEN_LOG = float(os.environ.get("MAKEFUN_SIZE_WIDEN", "0.45"))


def parse_time(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def minutes_between(a, b):
    return (b - a).total_seconds() / 60.0


def log10_sol(lamports):
    return math.log10(int(lamports)) - 9.0


def age_bucket(age_min):
    if age_min < 30:
        return 0
    if age_min < 60:
        return 1
    if age_min < 120:
        return 2
    if age_min < 240:
        return 3
    if age_min < 480:
        return 4
    return 5


def clamp(x, lo, hi):
    return lo if x < lo else hi if x > hi else x


def ols_slope(xs, ys):
    n = len(xs)
    if n < 2:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    den = sum((x - mx) ** 2 for x in xs)
    if den == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den


def main():
    in_dir = os.environ.get("MAKEFUN_INPUT_DIR", "/input")
    out_dir = os.environ.get("MAKEFUN_OUTPUT_DIR", "/output")
    with open(os.path.join(in_dir, "task.json"), "r", encoding="utf-8") as f:
        task = json.load(f)
    places = int(task["places"])
    edges = [float(e) for e in task["size"]["edges"]]
    n_bins = len(edges) + 1
    reads = [parse_time(r) for r in task["reads"]]
    t0 = parse_time(task["t0"])
    out_name = task.get("output", "forecast.json")

    marks = []
    with open(os.path.join(in_dir, "history.ndjson"), "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            m = json.loads(line)
            if m.get("ok") is True and isinstance(m.get("rows"), list) and m["rows"]:
                marks.append(m)
    if not marks:
        raise SystemExit("the history holds no ok mark with rows")
    marks.sort(key=lambda m: m["at"])

    # ---- per-coin series: mint -> list of (t_minutes_since_t0, log10cap, rank, venue) ----
    series = {}
    created_at = {}
    last_mark = marks[-1]
    last_t = parse_time(last_mark["at"])
    for m in marks:
        mt = parse_time(m["at"])
        tmin = minutes_between(t0, mt)
        for r in m["rows"]:
            mint = r["mint"]
            created_at.setdefault(mint, parse_time(r["created_at"]))
            series.setdefault(mint, []).append(
                (tmin, log10_sol(r["market_cap_lamports"]), int(r["rank"]), r.get("venue", "curve"))
            )
    for mint in series:
        series[mint].sort()

    # ---- pooled 10-minute log-changes by age bucket: drift prior + volatility ----
    bucket_changes = [[] for _ in range(6)]
    all_changes = []
    for mint, pts in series.items():
        for (t1, y1, _, _), (t2, y2, _, _) in zip(pts, pts[1:]):
            dt = t2 - t1
            if dt <= 0:
                continue
            age = minutes_between(created_at[mint], t0) + t1
            d = (y2 - y1) / (dt / 10.0)
            bucket_changes[age_bucket(age)].append(d)
            all_changes.append(d)

    def mean(xs):
        return sum(xs) / len(xs) if xs else 0.0

    def sd(xs, mu):
        return math.sqrt(sum((x - mu) ** 2 for x in xs) / len(xs)) if len(xs) > 1 else 0.13

    g_mean = mean(all_changes)
    g_sd = sd(all_changes, g_mean) if all_changes else 0.13
    bucket_prior = []
    bucket_vol = []
    for b in range(6):
        xs = bucket_changes[b]
        if len(xs) >= 20:
            mu = mean(xs)
            bucket_prior.append(mu)
            bucket_vol.append(clamp(sd(xs, mu), 0.03, 0.40))
        else:
            bucket_prior.append(g_mean)
            bucket_vol.append(g_sd)

    # ---- top-5-level cap threshold + contender (newcomer) statistics ----
    fifth_caps = []
    for m in marks:
        rows = sorted(m["rows"], key=lambda r: r["rank"])
        if len(rows) >= 5:
            fifth_caps.append(log10_sol(rows[4]["market_cap_lamports"]))
    fifth_caps.sort()
    top5_level = fifth_caps[len(fifth_caps) // 2] if fifth_caps else 3.0

    # Contenders: coins that reached top-5-level caps within 60 minutes of
    # launch. Each contributes its peak log-cap and the age at that peak.
    # Their rate per hour sets how many newcomer ghosts compete at a read.
    contender_peaks = []
    for mint, pts in series.items():
        c0 = created_at[mint]
        best = None
        for tmin, y, _, _ in pts:
            age = tmin - minutes_between(t0, c0)
            if 0 <= age <= 60 and (best is None or y > best[0]):
                best = (y, age)
        if best is not None and best[0] >= top5_level:
            contender_peaks.append(best)
    span_h = max(1.0, minutes_between(t0, last_t) / 60.0)
    contender_rate_per_h = len(contender_peaks) / span_h
    if not contender_peaks:
        contender_peaks = [(top5_level, 20.0)]
        contender_rate_per_h = 1.0

    # ---- candidate coins: last mark's rows + recent top-50 coins ----
    cand_mints = set(r["mint"] for r in last_mark["rows"])
    for m in marks[-6:]:
        for r in m["rows"]:
            if int(r["rank"]) <= 50:
                cand_mints.add(r["mint"])
    last_rank = {r["mint"]: int(r["rank"]) for r in last_mark["rows"]}
    last_y = {}
    last_tmin = {}
    for mint in cand_mints:
        pts = series[mint]
        last_y[mint] = pts[-1][1]
        last_tmin[mint] = pts[-1][0]
    cut_tmin = minutes_between(t0, last_t)

    # per-coin drift / vol at the cut
    cand = []
    for mint in sorted(cand_mints):
        pts = series[mint]
        age_cut = minutes_between(created_at[mint], last_t)
        b = age_bucket(age_cut)
        prior = bucket_prior[b]
        vol = bucket_vol[b]
        recent = pts[-4:]
        slope = ols_slope([p[0] for p in recent], [p[1] for p in recent])
        if slope is None:
            drift = prior
        else:
            w = len(recent) / (len(recent) + 3.0)
            drift = clamp(w * slope + (1.0 - w) * prior, -0.30, 0.30)
        if pts[-1][3] == "curve":
            vol = vol * 1.5
        cand.append(
            {
                "mint": mint,
                "y0": last_y[mint],
                "t0": last_tmin[mint],
                "age_cut": age_cut,
                "drift": drift,
                "vol": clamp(vol, 0.03, 0.40),
                "rank": last_rank.get(mint, 999),
            }
        )

    # ---- Monte Carlo over the answer reads ----
    rng = random.Random(SEED)
    n_reads = len(reads)
    place_counts = {}  # (mint, place) -> sims
    size_counts = {}  # (place, bin) -> sims
    top5_counts = {}  # mint -> sims in top 5 (any place)
    total_sims = NSIM * n_reads

    def bin_of(y):
        lo, hi = 0, len(edges)
        while lo < hi:
            mid = (lo + hi) >> 1
            if edges[mid] <= y:
                lo = mid + 1
            else:
                hi = mid
        return lo

    for read in reads:
        read_tmin = minutes_between(t0, read)
        # precompute per-coin (mu, sigma) at this read
        params = []
        for c in cand:
            steps = max(0.0, (read_tmin - c["t0"]) / 10.0)
            change = clamp(c["drift"] * steps, -1.5, 0.6)
            mu = c["y0"] + change
            sig = math.sqrt(0.03 ** 2 + (c["vol"] ** 2) * steps)
            params.append((c["mint"], mu, min(sig, 0.9)))
        # ghosts: newcomers launched in the hour before this read, redrawn
        # every simulation (each simulation is one possible future). A ghost
        # is a contender sampled from the history's peak caps, aged to a
        # random age at the read and decayed from its peak since then.
        lam = contender_rate_per_h * 1.0
        for _ in range(NSIM):
            items = []  # (logcap, mint or None)
            for mint, mu, sig in params:
                items.append((rng.gauss(mu, sig), mint))
            n_ghosts = 0
            if lam > 0:
                # Poisson by Knuth (lam is small, < ~40)
                L = math.exp(-lam)
                p = 1.0
                k = 0
                while True:
                    p *= rng.random()
                    if p <= L:
                        break
                    k += 1
                    if k > 60:
                        break
                n_ghosts = min(k, 60)
            for _ in range(n_ghosts):
                peak, peak_age = contender_peaks[rng.randrange(len(contender_peaks))]
                age_at_read = rng.uniform(5.0, 60.0)
                decay = clamp(-0.016 * (age_at_read - peak_age) / 10.0, -0.5, 0.2)
                items.append((peak + decay + rng.gauss(0.0, 0.15), None))
            top = heapq.nlargest(places, items, key=lambda x: x[0])
            for k, (cap, mint) in enumerate(top, start=1):
                if mint is not None:
                    place_counts[(mint, k)] = place_counts.get((mint, k), 0) + 1
                    top5_counts[mint] = top5_counts.get(mint, 0) + 1
                size_counts[(k, bin_of(cap))] = size_counts.get((k, bin_of(cap)), 0) + 1

    # ---- probabilities: name only rows that beat the unnamed background ----
    def round_down_6(x):
        return math.floor(x * 1e6) / 1e6

    probabilities = []
    place_sums = {}
    # first pass: coins with raw p >= 0.005
    named = {k: {} for k in range(1, places + 1)}
    for (mint, k), cnt in sorted(place_counts.items()):
        p = cnt / total_sims
        if p >= 0.005:
            named[k][mint] = p
    for k in range(1, places + 1):
        # background per unnamed chart coin, assuming ~100 - len(named) unnamed
        n_named = len(named[k])
        denom = max(5, 100 - n_named)
        s = sum(named[k].values())
        u = max(0.0, 1.0 - s) / denom
        keep = {m: p for m, p in named[k].items() if p > u}
        # if dropping pushed the sum down a lot, re-check once (one pass is enough)
        named[k] = keep
        place_sums[k] = sum(keep.values())
    # p=0 rows: dead coins, to concentrate the unnamed mass
    dead = []
    for c in cand:
        mint = c["mint"]
        if top5_counts.get(mint, 0) == 0 and c["rank"] > 100 and c["age_cut"] > 45:
            dead.append(mint)
    dead.sort()
    for k in range(1, places + 1):
        rows_here = 0
        for mint, p in sorted(named[k].items(), key=lambda kv: -kv[1]):
            probabilities.append({"mint": mint, "place": k, "p": round_down_6(p)})
            rows_here += 1
        for mint in dead:
            if rows_here >= 100:
                break
            if mint in named[k]:
                continue
            probabilities.append({"mint": mint, "place": k, "p": 0})
            rows_here += 1

    # ---- sizes: bins above background only ----
    def widen(dist, sigma_log, n_bins):
        """Convolve a bin distribution with a Gaussian of sd sigma_log."""
        if sigma_log <= 0:
            return dict(dist)
        sigma_bins = sigma_log / 0.05
        radius = max(1, int(math.ceil(3.0 * sigma_bins)))
        kern = [math.exp(-(d * d) / (2.0 * sigma_bins * sigma_bins))
                for d in range(-radius, radius + 1)]
        ks = sum(kern)
        kern = [k / ks for k in kern]
        out = {}
        for b, p in dist.items():
            for i, kv in enumerate(kern):
                nb = b + (i - radius)
                if 0 <= nb < n_bins:
                    out[nb] = out.get(nb, 0.0) + p * kv
        tot_in = sum(dist.values())
        tot_out = sum(out.values())
        if tot_out > 0 and tot_in > 0:
            f = tot_in / tot_out
            out = {b: p * f for b, p in out.items()}
        return out

    sizes = []
    for k in range(1, places + 1):
        raw = {}
        for (kk, b), cnt in size_counts.items():
            if kk == k:
                raw[b] = cnt / total_sims
        raw = widen(raw, SIZE_WIDEN_LOG, n_bins)
        s = sum(p for p in raw.values() if p >= 0.002)
        u = max(0.0, 1.0 - s) / n_bins
        for b in sorted(raw):
            p = raw[b]
            if p >= 0.002 and p > u:
                sizes.append({"place": k, "bin": b, "p": round_down_6(p)})

    # ---- top5: lowest expected place ----
    exp_place = {}
    for c in cand:
        mint = c["mint"]
        tot = sum(place_counts.get((mint, k), 0) for k in range(1, places + 1)) / total_sims
        expv = sum(k * place_counts.get((mint, k), 0) for k in range(1, places + 1)) / total_sims
        expv += (places + 1) * (1.0 - tot)
        exp_place[mint] = expv
    top5 = [m for m, _ in sorted(exp_place.items(), key=lambda kv: (kv[1], kv[0]))[:places]]

    forecast = {"probabilities": probabilities, "top5": top5, "sizes": sizes}
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, out_name), "w", encoding="utf-8") as f:
        f.write(json.dumps(forecast, separators=(",", ":")) + "\n")


if __name__ == "__main__":
    main()
