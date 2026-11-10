# Ranker v2: evidence

This folder holds the record behind `app/openfeed/ranker.py`. It contains the plan written
before any result existed, every deviation from it, the literature notes, the two
experiment reports and the sealed verification. Paths inside the reports that start with
`$S` or `/tmp/...` point to the scratch workspace where the work ran. That workspace is not
kept, and neither are the testbed data (a masked copy of the library and KuaiRec) or the
harness code.

| File | What it is |
| --- | --- |
| [preregistration.md](preregistration.md) | Questions, testbeds, metrics, statistics and controls. Written before any result. |
| [deviations.md](deviations.md) | Append-only list of deviations (D1–D17), each logged before the result it affects. D16 is the shipping rule. |
| [diagnosis.md](diagnosis.md) | Forensic look at the old ranker. Personal names and titles are redacted. |
| [research-*.md](research-retrieval.md) | Literature notes: retrieval, signals, re-ranking, methodology. |
| [track-a-retrieval.md](track-a-retrieval.md) | Experiments on graph retrieval and fusion (DEV only). |
| [track-b-user-model.md](track-b-user-model.md) | Experiments on the sparse-history user model and page re-ranking (DEV only). |
| Verification | This file, below. |
| [feel-preregistration.md](feel-preregistration.md) | The feel audit after the sealed test: targets written down first, two amendments, blind-judge and relevance results, and the fixes from an independent code audit. |

## Testbeds in one paragraph each

**T1 (own library, channel holdout).** One subscribed channel is hidden at a time, and the
question is whether its videos rank high among about 25k other videos from the same weeks.
Channel names, crawl paths and other label leaks are masked (D4, D6, D14). 23 channels are
DEV and 20 are SEALED. T1 is too small to decide anything (D15): a ranker deliberately
degraded by shuffling 60% of its top 100 was detected in only 2 of 20 seeds.

**T2 (KuaiRec small matrix).** This is a nearly fully observed user×video matrix. Each
simulated user reveals 5, 10 or 20 events, and the rest of their row is the truth. There are
988 DEV users and 423 SEALED. T2 decides modelling questions, but because it is a
short-video app it says nothing about magnitudes for OpenFeed.

The negative, positive, A/A and leak controls passed on T1. The degraded-ranker control
failed there, as stated above.

## Sealed verification (run once per candidate by a verifier who did not tune)

The candidates and the rule were fixed in D16 before any sealed read:

- RC1 is the related-list graph walk.
- RC2 is the same walk blended 0.25/0.75 with a views-and-age prior.

Both are compared with the best tuned baseline (`graph_walk`), with Holm correction across
the two. The first RC1 read on T2 crashed before scoring any unit. It was retried once under
D17, and the failed attempt stays in the log. The hash chain of the log was verified intact.

nDCG@20 on the sealed units:

| System | T1 (20 channels) | T2 n=5 | T2 n=10 | T2 n=20 |
| --- | --- | --- | --- | --- |
| Old ranker (2,000-row pool) | 0.004 | 0.043 | 0.043 | 0.047 |
| Random | 0.004 | 0.047 | 0.044 | 0.045 |
| Popularity | 0.021 | 0.291 | 0.288 | 0.287 |
| graph_walk (best baseline) | 0.041 | 0.296 | 0.325 | 0.350 |
| **RC1, graph** | **0.116** | 0.296 | 0.307 | 0.315 |
| **RC2, graph + prior** | 0.062 | **0.347** | **0.356** | **0.372** |

The contrasts against graph_walk, with the Holm-adjusted p and the 95% CI:

| | Contrast vs graph_walk |
| --- | --- |
| T1, RC1 | +0.075 (+184%), p=.38, CI [−0.008, +0.196] |
| T1, RC2 | +0.021, p=.58 |
| T2, RC1 | 0 at n=5; −5.5% at n=10; −10.1% at n=20 (both p<.001) |
| T2, RC2 | +17.2% at n=5; +9.6% at n=10; +6.2% at n=20 (all p<.001, CIs exclude 0) |

**The D16 decision, applied mechanically, ships RC1 (Graph) as the default**, labelled
"retrieval fix, no proven ranking lift". RC2 passed its accuracy clause at n=10. It failed
two guardrails as the verifier read them:

- The median age of the top 50 rose 11.4%. The pre-registration gives no direction for age.
- p95 harness scoring time was 64 ms, against 6 ms for the old scorer's harness time. In
  production the old path takes about 800 ms per call, and v2 takes 71–75 ms for a whole
  home load.

Both readings are debatable, but they were not reinterpreted after the results came in.
RC2 remains selectable as *Graph + popularity*.

What the evidence supports:

1. Both v2 candidates beat the old ranker by a wide margin on every sealed testbed. The old
   ranker scored at random level on T1 and at about 0.04 on T2, against 0.31–0.37 for v2.
   This was not the pre-registered deciding contrast, which used the strongest baseline
   instead.
2. Against the strongest simple baseline, only RC2 on T2 shows a significant gain. On
   OpenFeed's own library the graph alone ranked higher in every look:
   - T1 DEV: 0.082 vs 0.069
   - T1 SEALED: 0.116 vs 0.062
   - live leave-one-channel-out on the dev copy: 0.086 vs 0.025

   None of these differences is significant, and T1 lacks power.
3. Page policy is not an accuracy claim. Removing live streams and Shorts takes the live
   share of the top 50 from about 25% to about 3%.

## What is still unproven

- Seeding from the user's own watches. Neither testbed has OpenFeed-style watch events, so
  the n/(n+20) shrinkage comes from T2, where it was a modelling result, not an OpenFeed
  measurement.
- The negatives: the channel demotion after 2 or more negatives, and the exclusions. T2
  could not test them (the rule fires for 4 of 988 users).
- MMR combined with the 0.8^k creator factor. Each was tested alone on T2. The combination
  was not tested.

Impressions are now logged to `feed_impressions`. The next real test is online:
interleaving Graph against Graph + popularity on actual use, as
[research-reranking.md](research-reranking.md) H9 describes. That comparison now exists as
the opt-in "Help tune recommendations" setting (team-draft interleaving, clicks credited
per side, a Wilson interval on Status); it has no data yet.

## Additions after the feel audit (2026-11-08), and what they did to LOCO

These are product changes. None was tuned on a relevance metric, and none changes the
default Recommended mix.

- **Feed controls in Graph mode.** The defaults reproduce the shipped mix exactly: LOCO
  0.1077 plain and 0.2172 age-matched before and after on the 81,847-video library, and
  0.1077 and 0.2164 for both on the 81,926-video library a few hours later. The bounds were
  then set so each control alone keeps plain LOCO within 10% of the default. First bounds
  tried and rejected: "How far to wander" up to a 0.45 creators share plus 10% unreached
  slots (plain −13%, age-matched −21%) and a 0.6 channels factor for "Prioritize
  subscriptions" off (plain −12%). Shipped, on the 81,926-video library: 0.40 and 5% (plain
  0.0992, −8%; age-matched 0.1833) and 0.8 (plain 0.1000, −7%; age-matched 0.1973). Both at
  their exploring end: plain 0.0957 (−11%). LOCO rewards closeness to followed channels, so a
  control meant to wander away from them is bound to cost there.
- **Imported Takeout history, Up next, the Shorts shelf and the deeper Home lanes** do not
  enter LOCO (it seeds subscriptions only and ranks the Recommended lane). Up next was checked
  by reading real watch pages against each video's stored related list; that is a product
  check, not a measurement.
