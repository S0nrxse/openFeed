# Pre-registration 001: OpenFeed ranker v2 (written before any result)
date: 2026-11-07   author: lead   code base: scratch commit 0ba334a1895efbeded9e94c67f118b9fbf622eef

## Questions
Q1 retrieval: does a new pipeline surface videos a user independently chose better than the best simple method?
Q2 user model: under sparse history (5, 10, 20 events), which profile construction predicts satisfaction best?
Q3 page: does re-ranking improve diversity and calibration without a material loss in accuracy?

## Testbeds
T1 (own corpus, channel holdout). Unit: subscribed channel. 20 channels SEALED, chosen by sha256(channel_id + 'openfeed-prereg-001') lowest 20, stratified
   only if strata can be computed from video counts. The rest are DEV with nested CV (5 outer, 4 inner). SEALED channels are absent from DEV in every role: profile input,
   positive, negative, tuning. Positives: held-out channel's videos. Pool: all non-subscribed videos plus positives, age-matched to the positives.
   Masked: channel name/id terms (in the candidate text and the profile), RSS-vs-discovery source, crawl-path features. An edge between a held-out
   channel and a profile video counts only if it does not leak the label by construction (document how). No negative sampling: full ranking.
T2 (KuaiRec small matrix, nearly fully observed). Unit: user. 30% of users SEALED by hash. Each simulated user reveals only n events in chronological order (n in 5, 10, 20); the rest of their row is
   the unbiased outcome. Binary satisfied label: verify the definition from the paper (watch ratio threshold), and do not choose it from results. Captions are Chinese; use char n-grams
   plus category. Caveat kept with every result: short-video app, different population, so T2 decides modelling questions, not magnitudes for OpenFeed.
T3 (real negatives, guardrail). The real user's hides and not-interested videos must not be promoted. T3 is too small for inference.

## Metrics
Primary: nDCG@20 per unit (channel on T1, user on T2). Secondary: recall@100, MRR. Guardrails: intra-list similarity, channel Gini in top 50, calibration KL against subscription topic mix,
median age of top 50, share of live streams, share of Shorts, p95 latency, hidden-item rate. Allowed guardrail regressions: 10% relative, except latency, which must not exceed today's.

## Contrast
New pipeline versus the BEST tuned baseline, not versus the crippled current ranker. Baselines: random, popularity, velocity, recency, current ranker with its real pool, current ranker
scorer on the full pool, TF-IDF centroid, graph walk. Each baseline gets the same tuning budget. Tuning uses inner folds only.
Reported per-hypothesis, with absolute values and relative lift. Minimum effect of interest: +10% relative on nDCG@20.

## Statistics
Paired sign-flip randomisation test, 100,000 permutations, two-sided, plus paired bootstrap 95% CI, per unit. Holm across the contrasts of each question. BH across guardrails.
A result counts only if Holm-adjusted p < .05, the CI excludes 0, and guardrails pass. Null results get reported as such.

## Controls, which must pass before anything else is read
Negative control: shuffled labels give nDCG@20 consistent with random. Positive control: an oracle gets 1.0. Leak control: a channel-name-only model must not beat random after masking.
A/A: two random seeds of the same system show no difference. A failed control blocks all conclusions until it is explained.

## Sealed rule
Sealed units are read once per release candidate, at most 2 candidates, by a verifier who did not tune. All sealed runs get reported, not only the last.
Deviations are appended here with reasons. Nothing above is edited after results exist.
