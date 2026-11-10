# Evaluating the OpenFeed ranker with almost no interaction data

Status: research note, not implemented. Evidence tags: [read] = I read the abstract/paper or fetched the page; [secondary] = from search summaries only; [measured] = I ran it or queried the dev DB (scrubbed copy of production). Power code: alg/research/power.py.

## 1. Which known failures apply
- Ferrari Dacrema 2019 (arXiv 1907.06902 [read abstract]): neural recommenders were often beaten by tuned simple baselines. Applies:
  our "new ranker" must beat tuned popularity, recency, TF-IDF-nearest-profile, and the graph-only baseline, each given equal tuning budget.
- Rendle 2019 (arXiv 1905.01395 [read abstract]): baselines were badly run (ML-10M); a well-tuned plain MF beat reported SOTA. Applies:
  same rule; log the tuning budget per system in the pre-registration.
- Krichene and Rendle, KDD 2020 [secondary: IJCAI abstract/summary]: sampled metrics (ranking vs. a few random negatives) can reorder
  systems; small samples collapse toward AUC. Applies: rank against the whole matched pool, never 99 sampled negatives.
- Ji et al. 2023 (arXiv 2010.11060 [read abstract]): train/test splits ignoring the global timeline leak future information. Applies to
  any evaluation using watch history or time-varying signals (view counts, related-graph edges crawled today). Use time-masked features.
- Canamares and Castells SIGIR 2018 [secondary]: popularity can look strong or weak depending on how the test set was observed; it can be
  worse than random in some regimes. Bellogin, Castells, Cantador, IRJ 2017 [secondary]: sparsity and popularity bias distort IR metrics
  in recommendation; they propose designs that neutralise it. Applies: our positives come from a subscription process (popular channels
  subscribed more) so report popularity-stratified results and always include the popularity baseline.
- Jeunen RecSys 2019 [read first page]: calls for temporal evaluation, off-policy evaluation, and signals beyond clicks. Applies as the
  roadmap: offline proxies now, logged randomised exposure later.
- Ihemelandu and Ekstrand 2021 (arXiv 2109.06424 [read abstract]): recsys papers rarely use statistical inference properly. Applies to section 6.

## 2. External testbed datasets (HEAD checks run today, 2026-11-06)
| Dataset | Size / license | Text? | Downloadable now | Question it answers |
|---|---|---|---|---|
| KuaiRec | 1,411 users x 3,327 videos, 4.68M interactions, 99.6% dense small matrix (+12.5M-row big matrix); Zenodo says CC-BY-4.0, GitHub LICENSE says CC BY-SA 4.0 (cite both, keep attribution) | Captions + 3-level category IDs + tags, but Chinese (Kuaishou). TERM_RE (ASCII) gives nothing; needs a multilingual embedding or the category/tag IDs | Yes: 200 on https://zenodo.org/records/18164998/files/KuaiRec.zip (431,964,858 B), caption CSV 1,964,578 B | Almost-fully-observed ground truth, so it can test whether our protocol (sampled pool, missing negatives) picks the same winner as the full matrix |
| KuaiRand-Pure | 27,285 users, 7,583 videos, 47 MB; CC-BY-4.0 (Zenodo) | Captions/categories CSVs (Chinese) | Yes: https://zenodo.org/records/10439422/files/KuaiRand-Pure.tar.gz (47,432,272 B) | Randomly exposed items (about 0.37% of interactions) give unbiased exposure; tests IPS/OPE estimators later |
| MicroLens-100k | 100k users, ~19 MB pairs CSV, 1.5 MB English titles; research license, no re-hosting | `MicroLens-100k_title_en.csv` English but machine-translated and noisy (e.g. "Gu long song gaga bad") | Yes (200): https://recsys.westlake.edu.cn/MicroLens-100k-Dataset/MicroLens-100k_title_en.csv and `..._pairs.csv`; directory pages return 403 so use file URLs | Does a text-only content ranker beat popularity on real micro-video behaviour? |
| MIND-small | 50k users, English titles/abstracts, real impressions with non-click labels; Microsoft Research license | Yes, good English | Not verifiable: blob URL gives 409 (public access off), HF mirror 401. The portal https://msnews.github.io/ needs a licence click-through | Only dataset with true impressions (clicks vs shown-not-clicked), but news not video |
| MovieLens + tag genome | ml-25m 262 MB (genome inside); ml-latest-small 0.98 MB has no genome; GroupLens research-only | Tag relevance vectors, titles | Yes: https://files.grouplens.org/datasets/movielens/ml-25m.zip (261,978,986 B) | Generic content-vs-CF sanity check; old, popularity-heavy |
| YouTube-8M | Video-level features only, no titles or text of videos | No usable text | Index page 302 -> 200; bucket listing 403 | Not useful: no text, no user behaviour |
Pick: (1) KuaiRec for protocol validation (meta-evaluation): treat each of the 1,411 users as an independent N=1 experiment, subsample each
user to about 5-30 positives to mimic our sparsity, and check whether metrics computed on the subsample rank systems like the full matrix does.
Positives: the KuaiRec paper's watch_ratio threshold (I recall >2; verify in the paper before use). (2) MicroLens-100k for a
text-only ranker sanity check. Neither says anything about YouTube relevance; they only validate the method and the metrics. Do not
tune the production ranker on them.

## 3. Proxy ground truth on our own corpus
Facts [measured, dev DB]: 81 subscribed feeds (`feeds`), 0 takeout subscriptions; videos: 80,104 discovery (27,200 channels) vs 1,937
subscription-sourced rows from only 45 channels. So the 81 subscriptions are not all represented by RSS rows, and the crawl path
differs by label. That is the biggest leak: a model could learn "came via RSS" or "high related-graph in-degree from my subscriptions".
Design (leave-channels-out, LCO):
- Unit = channel. Positives for held-out channel c = its videos in the corpus (any source_kind). Revealed-preference assumption: a
  subscription means "would want this channel's videos". Valid for a ranker's content-affinity component, not for watch-time or mood.
- Profile for c = the other 80 subscriptions (their videos, the user's rare explicit events excluding c). Candidates = all corpus videos
  not from any subscribed channel except c, plus c's videos. Full ranking, no sampling.
- Leakage controls: (1) mask channel name, handle and author strings from title/description/tags of every video (and from c's text in
  other videos); (2) drop video_discoveries edges whose both ends are in c; keep edges from profile videos to c's videos and report
  with/without as an ablation; (3) do not use source_kind, discovery_source, discovered_at, created_at or any crawl-order feature;
  (4) match pool and positives on age (published_at within the same window) because RSS yields only the latest items and a recency
  baseline would otherwise win for the wrong reason; also bucket by view count; (5) TF-IDF/embedding fit may use all text (unsupervised),
  but every learned weight or hyperparameter is fit only on training channels; (6) a "channel-ID oracle" probe: train a text classifier
  to predict channel from masked text; if accuracy is far above chance the mask failed.
- False negatives: unsubscribed videos the user would enjoy count as misses. Absolute numbers are therefore low and uninterpretable;
  only paired differences between systems are meaningful, and a system that finds genuinely good new content is penalised.
- Popularity confound: subscribed channels are popular; report results stratified by channel size tercile.
- Negative labels: 8 not_interested + 15 hide = 23 events. Too few for training or a primary metric. Use only as a guardrail: the
  fraction of hidden items placed in the top 50 should be 0 (one-sided exact binomial vs. the pool rate).
- What it can show: the ranker transfers affinity from known channels to unseen channels better than baselines, with n=81 units.
  What it cannot show: that the user will watch more, that new discovery is wanted, long-term satisfaction, or anything about exposure.
Extra label sources, best first:
 a. Blind pooled rating session: pool the top 20 from every system plus 20 random, shuffle, hide system identity, user labels
    each 0/1/2 (interesting / neutral / no). 150 items is about 15 minutes; it gives true pairwise judgments (TREC-style pooling) and
    covers false negatives. Do this for the sealed test.
 b. Temporal-forward on subscription order if `user_subscriptions` has created_at: train on the first t subscriptions, rank the
    next ones (Ji et al., Jeunen). Check the column first.
 c. subscription_collection_members: user-made groups; predict held-out channel's collection (clustering ground truth).
 d. takeout_watched_videos if the user imports history (0 rows now); playback_progress (12 rows) and 55 watch_page_open as weak,
    exposure-confounded positives, sanity checks only.
 e. Not available: feed_impressions is empty, so no CTR yet; start logging with random exposure now (section 5).

## 4. Simulation
RecSim (Ie et al. 2019, arXiv 1909.04847 [read abstract]) is a configurable simulator for sequential interaction. Chaney et al.
(arXiv 1710.11214 [read abstract]) show with simulation that training on feedback-loop data homogenises behaviour without raising
utility. Search results [secondary] add: LLM user simulators can leak targets and inflate scores; one shared choice model for all
users reduces realism (Ungruh, Bellogin, Pera, SIGIR 2025). I could not find a "Sim4Rec" source and make no claim about it.
- Informative: testing mechanisms (does negative propagation cause filter bubbles, does diversity capping hurt calibration under
  a stated user model), stress tests for code paths, checking estimators (IPS bias/variance) where the truth is known by construction.
- Circular: any claim that ranker X "is better for users" when the simulated user's utility is built from the same features the ranker
  uses (channel clusters -> persona -> ranker clusters). The ranker wins by construction.
- Safe use: build personas from a source the ranker cannot see (e.g. KuaiRec's full matrix, or MovieLens genome labels held out), pre-register
  the persona set before seeing results, vary the user-model family, and report only rank-ordering robustness across model families.
  Persona-from-channel-cluster simulations are allowed for estimator tests and regressions, never as evidence of improvement.

## 5. Off-policy evaluation, later
IPS = mean(w*r), w = pi_new/pi_log; SNIPS divides by mean(w) (lower variance, small bias); DR adds a reward model and is unbiased if
either the propensity or the model is right. All need logged propensities: log every impression with slot, candidate pool id and
probability (feed_impressions/recommendation_runs exist but are unused). Reserve epsilon = 10% of slots for uniform-random
choice from the top-200 so propensities are bounded below (w <= 1/epsilon in the worst case).
Rough power [my calculation, power.py; assumptions are guesses]: baseline open rate p0 = 8% per impression.
- iid impressions, two arms: 20% relative lift needs about 4,900 impressions per arm; 50% lift about 880; 10% lift about 18,900.
  At 3%: 13,900 per arm for 20%. At 15%: 2,400. These ignore within-day correlation, so they are lower bounds.
- Switchback, one user, a day per block, random A/B within pairs of days, sign-flip randomisation test on day differences,
  day-to-day logit sd 0.4: 40 impressions/day, 50% lift reaches 80% power at about 56 days (43% at 28 days); 20% lift only 45% at 112
  days. 100 impressions/day: 50% lift 77% at 28 days and 99% at 56 days; 20% lift 75% at 112 days.
Conclusion: one user can detect only large effects (about 50% relative) in 1-2 months; small gains (10-20%) are not testable online,
so offline LCO and pooled blind ratings must carry most decisions. Use carry-over washout (skip the first impressions after a switch).

## 6. Statistics
- Unit of analysis: held-out channel (LCO), user (KuaiRec), day (switchback). Never impressions or videos as independent units; videos
  within a channel are strongly correlated. Pairs: same unit scored by both systems.
- Test: paired randomisation (sign-flip) test on per-unit metric differences (Smucker, Allan, Carterette CIKM 2007 [secondary]:
  randomisation, bootstrap and t-test agree; recommend randomisation; discourage Wilcoxon/sign for mean differences). Paired bootstrap
  (10,000 resamples of units) for the 95% CI. Report mean paired difference with CI and a standardised effect (Cohen d_z), as Sakai
  argues for effect sizes plus CIs [secondary]. Wilcoxon only as a robustness check.
- Multiple comparisons: one pre-registered primary comparison (new vs current ranker) at alpha 0.05, no correction. Secondary
  comparisons (vs each other baseline): Holm over all of them. Guardrail and exploratory metrics: Benjamini-Hochberg at q=0.10, labelled exploratory.
- Few folds: repeated k-fold re-uses data so fold-level tests are invalid; test over units (channels), not folds. With a paired-diff
  sd of 0.15 nDCG (assumption, measure it), minimum detectable difference at 80% power: 0.094 with 20 sealed channels, 0.054 with 61 dev,
  0.047 with 81. Only big wins can be certified on the sealed set.
- Tuning vs sealed: before any experiment stratify the 81 channels by size tercile and seeded-random assign 20 to SEALED (store the seed and
  the sorted channel-id hash in the pre-reg). The 61 DEV channels use nested CV: outer 5 folds (12-13 channels) for estimation; inner 4-fold for
  hyperparameters, with a fixed equal search budget for every system. SEALED is scored once per release candidate chosen on DEV.
  If SEALED is opened a second time for the same hypothesis family, the result is exploratory and must say so.

## 7. Metrics
Primary: nDCG@20 over the full matched pool, binary relevance, per held-out channel, average over channels. Why: it is rank-aware, bounded,
top-weighted like a feed, and has the most direct IR significance literature behind it. Secondary: recall@50 and MRR of the first hit (robust
when a channel has few videos). MAP omitted (redundant with nDCG for binary labels).
Guardrails (a change must not regress beyond the stated margin on the top-20 list; judged by CI, not point estimate):
- Channel Gini and max channel share (diversity of sources); intra-list topic diversity (mean pairwise cosine distance); coverage (share of
  corpus channels appearing across all sessions); calibration KL(user topic dist || list topic dist); novelty (mean -log2 popularity);
  unexpectedness (distance from a primary-baseline's list); freshness (median age of the list, to detect a pure-recency collapse);
  hidden-item rate (section 3); latency p95 < current + 20%. Report but do not optimise coverage/novelty directly.

## 8. Concrete protocol (v1)
Splits: SEALED = 20 channels; DEV = 61 channels, nested 5x4. Secondary stress test: leave-cluster-out (k-means on channel text vectors, k=8).
Candidate pool: all videos not from subscribed channels (plus held-out channel), age-matched to positives, full ranking, no negative sampling.
Labels: positive = video from held-out channel. Guardrail-only: hide/not_interested. SEALED additionally gets a blind pooled human label (3.a).
Baselines (all tuned with the same budget): random (seeded, 100 draws); popularity (view_count, tie-break recency); recency; current ranker
(list_recommended_videos pipeline at commit hash X, with subscription-seeded profile off and on); TF-IDF cosine to channel-mean profile; related-graph
random-walk score from profile videos. New methods must beat the best tuned baseline, not only random.
Primary: nDCG@20, paired by channel. Test: sign-flip randomisation, 100,000 permutations, two-sided, plus paired bootstrap 95% CI.
Corrections: Holm across baselines; BH for guardrails. Significant = Holm-adjusted p < 0.05 and CI excludes 0 and guardrails not violated.
Sealed rule: no code touches SEALED labels until the pre-reg is committed; one run per candidate; results appended, never overwritten.
Online: when feed_impressions is logging with epsilon-random exposure, run switchback day-blocks for >= 56 days before trusting any claim under 50%.

Pre-registration template (commit as alg/prereg/NNN.md before running):
```
id/date/commit-hash of code and data snapshot:
hypothesis (one sentence, direction stated):
systems compared, and the one primary contrast (new vs ...):
split seed + SEALED channel-id hash:           tuning budget per system:
label definition (positive, pool, age matching, masked fields):
primary metric (nDCG@20, per-channel):         minimum effect of interest:
test + permutations + CI method:               multiplicity plan (Holm set / BH set):
guardrails and allowed regression margins:     stopping rule / max SEALED runs: 1
exclusions decided now (and nothing after):
deviations log (filled after, with reason):
```
