#!/usr/bin/env python3
"""Local validation + backtest for main.py.

1. Validates a forecast file against the competition's format rules
   (exact keys, row shapes, sums <= 1, top5 distinct, bins in range).
2. Scores a forecast on pseudo-reads (later marks of the practice
   history) with the canonical log-score rule from grader/forecast/ranked.ts
   and RULES.txt: order + size, floor 1e-4, unnamed mass shared over the
   chart's unnamed coins / bins.
3. Backtest: truncate the history at mark N, run a forecaster program on
   the truncated input, score it on the following marks as reads.
"""
import json, math, os, subprocess, sys, tempfile

FLOOR = 1e-4


def load_marks(path):
    marks = []
    for line in open(path):
        line = line.strip()
        if line:
            m = json.loads(line)
            if m.get("ok") is True and isinstance(m.get("rows"), list) and m["rows"]:
                marks.append(m)
    marks.sort(key=lambda m: m["at"])
    return marks


def validate(forecast, places=5, n_bins=82):
    assert set(forecast.keys()) == {"probabilities", "top5", "sizes"}, forecast.keys()
    assert isinstance(forecast["top5"], list) and len(forecast["top5"]) == places
    assert len(set(forecast["top5"])) == places
    sums = {}
    for r in forecast["probabilities"]:
        assert set(r.keys()) == {"mint", "place", "p"}, r
        assert 1 <= r["place"] <= places and 0 <= r["p"] <= 1
        assert isinstance(r["mint"], str) and 32 <= len(r["mint"]) <= 44
        sums[r["place"]] = sums.get(r["place"], 0) + r["p"]
    for k, s in sums.items():
        assert s <= 1 + 1e-9, (k, s)
    ssums = {}
    for r in forecast["sizes"]:
        assert set(r.keys()) == {"place", "bin", "p"}, r
        assert 1 <= r["place"] <= places and 0 <= r["bin"] < n_bins and 0 <= r["p"] <= 1
        ssums[r["place"]] = ssums.get(r["place"], 0) + r["p"]
    for k, s in ssums.items():
        assert s <= 1 + 1e-9, (k, s)
    return True


def score(forecast, read_charts, history_mints, edges):
    """read_charts: list of (chart_mints_in_order, place_caps_log10sol).
    Returns (mean_total, mean_order, mean_size, agreement)."""
    named = {k: {} for k in range(1, 6)}
    for r in forecast["probabilities"]:
        mint = r["mint"]
        if mint not in history_mints:
            continue  # dropped before scoring; p returns to unnamed share
        named[r["place"]][mint] = r["p"]
    sums = {k: sum(named[k].values()) for k in range(1, 6)}
    size_named = {k: {} for k in range(1, 6)}
    for r in forecast["sizes"]:
        size_named[r["place"]][r["bin"]] = r["p"]
    size_sums = {k: sum(size_named[k].values()) for k in range(1, 6)}
    n_bins = len(edges) + 1

    def bin_of(y):
        lo, hi = 0, len(edges)
        while lo < hi:
            mid = (lo + hi) >> 1
            if edges[mid] <= y:
                lo = mid + 1
            else:
                hi = mid
        return lo

    totals, orders, sizes, agrees = [], [], [], []
    for chart, caps in read_charts:
        o = 0.0
        for k in range(1, 6):
            mint = chart[k - 1]
            if mint in named[k]:
                p = named[k][mint]
            else:
                unnamed = sum(1 for c in set(chart) if c not in named[k])
                left = max(0.0, 1.0 - sums[k])
                p = left / unnamed if unnamed else 0.0
            o += math.log(max(p, FLOOR))
        s = 0.0
        for k in range(1, 6):
            b = bin_of(caps[k - 1])
            if b in size_named[k]:
                p = size_named[k][b]
            else:
                left = max(0.0, 1.0 - size_sums[k])
                p = left / (n_bins - len(size_named[k]))
            s += math.log(max(p, FLOOR))
        totals.append(o + s)
        orders.append(o)
        sizes.append(s)
        top = forecast["top5"]
        head = chart[:5]
        ag = 0
        for i, mint in enumerate(top):
            if mint in head:
                ag += 5 - abs(i - head.index(mint))
        agrees.append(ag)
    n = len(read_charts)
    return sum(totals) / n, sum(orders) / n, sum(sizes) / n, sum(agrees)


def run_program(prog_dir, input_dir, output_dir, extra_env=None):
    env = dict(os.environ)
    env["MAKEFUN_INPUT_DIR"] = input_dir
    env["MAKEFUN_OUTPUT_DIR"] = output_dir
    if extra_env:
        env.update(extra_env)
    os.makedirs(output_dir, exist_ok=True)
    r = subprocess.run([sys.executable, "main.py"], cwd=prog_dir, env=env,
                       capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        raise RuntimeError(f"program failed: {r.stderr[-2000:]}")
    with open(os.path.join(output_dir, "forecast.json")) as f:
        return json.load(f)


def run_baseline(kit_dir, input_dir, output_dir):
    env = dict(os.environ)
    env["MAKEFUN_INPUT_DIR"] = input_dir
    env["MAKEFUN_OUTPUT_DIR"] = output_dir
    os.makedirs(output_dir, exist_ok=True)
    r = subprocess.run(["node", "starter/main.ts"], cwd=kit_dir, env=env,
                       capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise RuntimeError(f"baseline failed: {r.stderr[-2000:]}")
    with open(os.path.join(output_dir, "forecast.json")) as f:
        return json.load(f)


WIDEN = os.environ.get("SWEEP_WIDEN", "0.45")


def main():
    kit = os.path.abspath("../kit")
    marks = load_marks(os.path.join(kit, "input/history.ndjson"))
    task = json.load(open(os.path.join(kit, "input/task.json")))
    edges = task["size"]["edges"]

    for cut in (1, 2, 3, 4):
        with tempfile.TemporaryDirectory() as td:
            in_dir = os.path.join(td, "in")
            os.makedirs(in_dir)
            trunc = marks[:cut]
            with open(os.path.join(in_dir, "history.ndjson"), "w") as f:
                for m in trunc:
                    f.write(json.dumps(m) + "\n")
            t2 = dict(task)
            t2["marks"] = len(trunc)
            t2["history_until"] = marks[cut]["at"]
            t2["reads"] = [m["at"] for m in marks[cut:]]
            with open(os.path.join(in_dir, "task.json"), "w") as f:
                json.dump(t2, f)
            hist_mints = {r["mint"] for m in trunc for r in m["rows"]}
            reads = []
            for m in marks[cut:]:
                rows = sorted(m["rows"], key=lambda r: r["rank"])[:100]
                chart = [r["mint"] for r in rows]
                caps = [math.log10(int(r["market_cap_lamports"])) - 9.0
                        for r in rows[:5]]
                reads.append((chart, caps))
            mine = run_program(".", in_dir, os.path.join(td, "out-mine"),
                               {"MAKEFUN_SIZE_WIDEN": WIDEN})
            base = run_baseline(kit, in_dir, os.path.join(td, "out-base"))
            validate(mine)
            validate(base)
            sm = score(mine, reads, hist_mints, edges)
            sb = score(base, reads, hist_mints, edges)
            print(f"cut after mark {cut} ({trunc[-1]['at']}), {len(reads)} read(s):")
            print(f"  mine:     total {sm[0]:.3f}  order {sm[1]:.3f}  size {sm[2]:.3f}  agree {sm[3]}")
            print(f"  baseline: total {sb[0]:.3f}  order {sb[1]:.3f}  size {sb[2]:.3f}  agree {sb[3]}")


if __name__ == "__main__":
    main()
