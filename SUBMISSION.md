# pumpfun-top5-forecast-01 — submission record

- Competition: pumpfun-top5-forecast-01 ("Predict the top 5 new Solana coins by market cap, 24 hours out")
- Reward: $200 USDC (1st $50, 2nd $30, 3rd $24, 4th $20, 5th $16, 6th-10th $12)
- Account: @meviusorg5799 (verified via GET /api/me); agent wallet 8u49tpfF9Q2F14AG64KR8gFuAumua9fow2aKBeYFm35W
- Repo: https://github.com/meviusorg5799/pumpfun-top5-forecast (public, created 2026-11-12T05:33:08Z)
- Commit submitted: 4ba6b1f27fd94f872ad6bedc80314645e004dadd
- Submission ID: 01a1ce40-4116-74e8-9473-9d443e21ac31 (accepted 2026-11-12T05:34:39Z)
- Entry: main.py — Monte Carlo forecaster (age-bucketed drift/volatility,
  newcomer ghost simulation, fixed-seed MC over the 7 answer reads,
  honest unnamed mass for coins born after the cut)
- Local backtest (truncated practice history): -26.31 / -21.75 vs house
  baseline -33.48 / -29.87 (two cuts) — beats baseline by ~7-8 nats.
- All intake + transcript checks pass on the server (Round 1); dev-phase
  grading was under way at last poll.
- Transcript bundle: transcripts.tar.gz sha256 7b42bb3b0ddfa4fcb0e37775e4681e511507df3f62610e8f6fdb70dad2a92e74
- Claim code: (see parent report; also retrievable via `makefun status --submission <id>`)
- Deadlines: entries close 2026-11-12T14:00:00Z (20:30 Yangon); final window
  starts 2026-11-13T02:00:00Z; result due 2026-11-27T08:00:00Z.
- Quota used: 1 of 10 submissions/day, 1 of 10 total. Resubmission possible
  before the deadline if a fix is verified.
