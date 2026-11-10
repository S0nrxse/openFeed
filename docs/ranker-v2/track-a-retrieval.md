# Track A report: candidate retrieval and score fusion (ranker v2, DEV only)

S = /tmp/claude-1000/-home-peyton-app-lab-projects-openFeed/b2720c98-0a1e-4338-968f-b34b3b443505/scratchpad
Code: $S/alg/exp/A/systems_A.py (systems), run_A.py / run_fixed.py (nested CV runs), compare.py (stats),
diag_t1.py / diag_head.py / diag2_t1.py (diagnostics, no tuning), ref_A.py (production reference), parity_ref.py.
Per-unit results: $S/alg/exp/A/results/{t1,t2_n10}/*.parquet; chosen configs are in the matching *.json; full tables are in
results/compare_t1.txt and results/compare_t2_n10.txt. All runs used harness runner.nested (5 outer x 4 inner folds),
the 24-config budget (runner.draw) and n_jobs=3. Sealed units were never touched.

## 0. Things the lead must know first

1. **Harness leak on T1 (reported mid-run).** `items.duration == 0` identifies live streams exactly. 22.75% of the eligible
   negatives have duration 0, and all of them are live. 0 of 985 DEV positives have duration 0. Live share is 25.0% of negatives and 0% of positives.
   D4 says live is hidden, but duration exposes it. There is also a structural leak: positives never have G1 out-edges (D6). Mean
   AUC of -outdegree across the 23 units is 0.64 (diag_t1.py). No accuracy system here reads duration or G1 out-degree. Only the explicit
   H5 variant uses duration==0, and its accuracy effect is labelled mechanical.
2. **The T2 baseline chain crashed (harness bug, reported).** TfidfCentroid.fit fails when wc=wk=0 (`self.X` is None, baselines.py:99).
   As a result there are no harness results for t2_n10 graph_walk or tfidf_centroid, and nothing for n5 or n20. I re-ran `B.GraphWalk` on T2 n10 with
   the identical nested protocol (results/t2_n10/graph_walk.parquet, 0.3256 > popularity 0.2906). That is the best tuned T2 baseline available.
3. **T1 baselines reproduce after the rebuild.** On the rebuilt T1 I re-ran graph_walk, velocity and popularity. graph_walk matches
   the 6 Nov parquet exactly (max abs per-unit diff 0.0).
4. **T1 power.** The RP3-vs-graph_walk per-unit difference has SD 0.218. The 80%-power MDE at n=23 is 0.127 nDCG@20, which is +247% of the baseline.
   Resampled power to detect the pre-registered +10% is 0.29. In every T1 system at least 15 of 23 units score 0, so one or two units decide
   the means. T1 cannot confirm or reject anything at the pre-registered size. Only T2 has the power for modelling questions, and it does not set magnitudes.

## 1. Results (nDCG@20 / recall@100 / MRR, means with unit-bootstrap 95% CI)

### T1 (23 DEV channels). Best tuned baseline: graph_walk.
| system | nDCG@20 [CI] | recall@100 | MRR | vs graph_walk: diff, rel, p (Holm), paired CI |
|---|---|---|---|---|
| graph_walk (baseline, rerun) | 0.0515 [0.011, 0.100] | 0.065 | 0.158 | n/a |
| velocity | 0.0423 [0, 0.114] | 0.063 | 0.095 | -0.009, -18%, p=.74 |
| popularity | 0.0319 [0, 0.083] | 0.090 | 0.034 | -0.020, -38%, p=.41 |
| tfidf_centroid (rerun) | 0.0000 | 0.019 | 0.008 | n/a |
| **A_rp3** (H1) | **0.0816** [0.017, 0.180] | **0.181** | 0.125 | +0.030, **+58%**, p=.76 (Holm 1.0), CI [-0.037, +0.129] |
| A_rp3 beta=0 / 0.3 / 0.6 | 0.0805 / 0.0813 / 0.0819 | 0.186 / 0.180 / 0.182 | 0.118 / 0.122 / 0.127 | all p>.76 |
| A_content (H2) | 0.0000 | 0.037 | 0.009 | -0.052, p=.015 (Holm .11) |
| A_fused_cc (H4, tuned on T1 inner folds) | 0.0202 [0.001, 0.052] | 0.097 | 0.062 | -0.031, -61%, p=.046 (Holm .19), CI [-0.070, -0.003] |
| A_fused_rrf (H4, k=60) | 0.0375 [0.002, 0.101] | 0.152 | 0.063 | -0.014, -27%, p=.60 |
| A_fused_gp_fixed (T2's choice transferred) | 0.0687 [0.012, 0.160] | 0.099 | 0.114 | +0.017, +33%, p=.67, CI [-0.046, +0.088] |
| A_rp3 + live/Shorts demote (H5) | 0.0816 | 0.189 | 0.125 | identical to A_rp3 (diff 0.0000) |

On recall@100 (secondary), A_rp3 scores 0.181 vs 0.065: diff +0.116, p=.056, CI [+0.013, +0.243]. Robustness check: RP3 beats graph_walk on 4 units,
loses on 4 and ties on 15. Without its single best unit (unit 17, 3 positives, nDCG 0.97) the mean difference is -0.013.

T1 guardrails (top 50, mean over units), graph_walk vs A_rp3: ILS 0.013 vs 0.012; channel Gini 0.080 vs 0.097 (+22%);
calib_KL 0.529 vs 0.585 (+11%); median age 69 vs 58 days; live share **0.238 vs 0.039**; Shorts 0 vs 0.027;
p95 score latency 9 vs 11 ms. With live/Shorts demote: live 0.007, Shorts 0, Gini 0.103, calib_KL 0.555. Gini and calib_KL are nominally
over the 10% allowance, but none of the differences is significant at n=23.

### T2 n10 (988 users). Best tuned baseline: graph_walk (rerun, see 0.2).
| system | nDCG@20 [CI] | recall@100 | MRR | vs graph_walk: diff, rel, p (Holm over 6), paired CI |
|---|---|---|---|---|
| graph_walk | 0.3256 [0.316, 0.336] | 0.162 | 0.683 | n/a |
| popularity (harness) | 0.2906 | 0.187 | 0.715 | -0.035, -10.8%, p<1e-5 |
| A_rp3 (a<=1) | 0.3000 | 0.160 | 0.591 | -0.026, -7.9%, p<1e-5 |
| A_content | 0.0562 | 0.034 | 0.150 | -0.269, -83% (random 0.047, recency 0.065) |
| **A_fused_cc** | **0.3557** [0.347, 0.365] | **0.190** | 0.681 | **+0.030, +9.2%, p<1e-5 (Holm 6e-5), CI [+0.025, +0.035]** |
| A_fused_cc_shrink (H3) | 0.3554 | 0.190 | 0.682 | +0.030, +9.1%; vs A_fused_cc -0.0003, p=.55 |
| A_fused_rrf | 0.3049 | 0.167 | 0.590 | -0.021, -6.4%, p<1e-5; CC beats RRF by +0.051 (+16.7%), p<1e-5 |

A_fused_cc chose the same config in 5 of 5 folds: min-max, graph 0.25, content 0, prior 0.75. The prior is the KuaiRec complete-play rate
(popularity stat c). Recall@100 rose +0.028 (+17%, p<1e-5); MRR was unchanged (-0.003, p=.70). Guardrails vs graph_walk: Gini 0.096 vs 0.152
(-37%, more even), calib_KL 1.567 vs 1.736 (-10%), ILS flat, median age +9% (inside 10%). Harness p95 latency is 54 ms, because content
is still computed at weight 0. The graph+prior path in ref_A takes 34-42 ms per user. Caveat that goes with every T2 number: this is a short-video app with a different
population, so T2 decides modelling questions, not magnitudes. The +9.2% is significant, but its point estimate is below the
pre-registered +10% MEI; the CI upper bound is +10.8%.

## 2. Hypotheses and verdicts

**H1. Graph: an RP3beta-style walk with degree discount** (retrieval.md section 2.2 and H5; Christoffel et al.; Pixie section 2.1). System `RP3`
builds a bipartite item x context graph. Contexts are the stored related-list ids (G2), plus G1 discovery sources in mode "both".
Each seed (subscription video) is also linked to its own id, so direct edges become 2-step paths, and each subscribed channel carries seed mass 1.
The walk goes item -> context -> item, with context weight 1/|context| ("prob") or log-idf, and an optional second round at half weight;
the score is then divided by item degree^beta. The space is mode{g2,both} x beta{0,.3,.6} x hub{prob,idf} x rounds{1,2}, 24 configs.
Choices across folds: hub=prob 5/5, g2 4/5, rounds=2 in 3/5, beta 0.3/0.6/0.3/0.6/0.
- T1: 0.0816 vs 0.0515, +58%, p=.76. **Null**: not significant, and one unit drives it. Recall@100 is roughly 3x (CI excludes 0, p=.056).
  The share of live streams in the top 50 falls from 24% to 4%. The walk reaches 63% of the pool and 93% of the positives (diag2_t1.py).
- Degree discount: with beta fixed, 0 / 0.3 / 0.6 score 0.0805 / 0.0813 / 0.0819, so it has no effect. On T2, graph_walk's item-degree exponent b
  was 0 in 5 of 5 folds, and so was my RP3's. **Verdict: degree discount does not help on either testbed.** Discounting the
  hub (context) does matter: "prob" won 5/5 on T1, and the T2 user-degree exponent a=1.4 won 5/5 over a<=1. That is why my RP3, with a<=1,
  trails graph_walk by 7.9% on T2.

**H2. Content: why TF-IDF scores 0, and BM25 or channel-exemplar max-sim as a fix** (retrieval.md H3/H4, signals.md F15).
- Diagnosis (diag_t1.py and diag_head.py; no tuning). Content does carry signal: mean per-unit AUC of the profile cosine is 0.69 on X_doc,
  0.69 on X_uni and 0.67 on X_title, and positives have higher mean cosine than negatives (0.051 vs 0.033). The head is the problem. The top 20 are near-clones of the most prolific
  visible subscriptions' genres; for example, police/bodycam videos dominate when two visible channels with 175-340 videos shape the centroid.
  With uni/chan_max, content's top 20 holds no positive for 20 of 23 units (diag2_t1.py). T1 asks for a *different* followed channel, and
  "same niche as a channel you already follow" is exactly what the discovery crawl harvested as negatives. 85% of the content top 20 lies in the
  profile's crawl neighbourhood, against 63% of the pool.
- System `ContentA`: x{doc, uni, title} x agg{chan_mean, chan_max} x background contrast bg{0, .5, 1}. The contrast subtracts bg x cosine to the
  corpus centroid. These variants cover channel-normalised profiles and per-channel exemplar max-sim, all seeded from subscriptions (H3).
  Result: nDCG@20 = 0.000. Item-level kNN max-sim was dropped after it ran for over 40 minutes on 25 train views. Literal BM25 was not run:
  the harness matrices are sublinear-TF, L2-normalised TF-IDF without raw counts, and retrieval.md section 1.1 rates BM25 saturation as weakly transferable
  to document-to-document similarity.
- T2: content alone scores 0.056 (random 0.047), and fusion tuned its weight to 0 in 5 of 5 folds.
- **Verdict: null on both testbeds, alone or fused.** Keep the TF-IDF fixes (Unicode tokeniser, stopwords) for explanations and
  UI, not as ranking weight.

**H3. A popularity/velocity prior shrunk by personal evidence** (retrieval.md H9; Ji et al. 2020).
- T2: the prior is the dominant fused component (0.75 against graph 0.25). Graph+prior beats graph alone by +9.2% and prior alone by +22%
  (0.356 vs 0.291, p<1e-5). Shrinking the prior weight by k/(k+n_pos), with k=5, changed nothing (-0.0003, p=.55). **Verdict: the prior
  helps in fusion; evidence-dependent shrinkage is null at n=10.** n5 and n20 were not run because their baselines are missing (0.2).
- T1: velocity alone (0.042) scores below graph_walk. T1 cannot test shrinkage, because every unit has the same profile of about 20 channels.

**H4. Fusion: per-signal normalised convex combination vs RRF with k=60** (retrieval.md section 4; Bruch et al. 2022 vs Cormack et al. 2009).
`FusedCC` searches norm{pct, minmax} x the 12 mixed points of the 0.25 simplex over (graph, content, prior), 24 configs. Component configs per
outer fold come from that fold's own inner folds (FOLD_PARAMS), so outer units never inform a choice.
- T2: **CC beats RRF by +0.051 (+16.7%, p<1e-5), and CC beats graph_walk by +9.2% (p<1e-5, significant after Holm).** RRF loses to graph_walk
  (-6.4%), because equal rank weighting gives the useless content signal a full vote, as Bruch et al. predict. Min-max beat percentile in 5 of 5 folds.
- T1: tuned CC scores 0.020, below graph_walk (-61%, p=.046 raw, Holm .19) and below RP3 (-75%, p=.031). The 4-5 channel inner folds
  picked graph+content (inner nDCG 0.07-0.14), and the outer folds did not reproduce it: overfitting. RRF scores 0.038 (p=.60).
  As a pre-specified transfer test, I applied T2's choice unchanged (min-max, graph .25, prior .75; run_fixed.py). It scores 0.0687:
  +33% vs graph_walk (p=.67) and -16% vs RP3 (p=.72). The prior pulls the median age of the top 50 from 58 down to 18 days.
- **Verdict: CC fusion of graph+prior is this track's only Holm-significant gain. It holds on T2 only, at +9.2%, just under the MEI.
  On T1 it is null, and tuning fusion weights on T1 actively hurts. RRF is worse than CC on both testbeds.**

**H5. Live-stream and Shorts handling** (diagnosis.md section 4, defect 6). Demoting duration==0 (live) and 0<duration<=90 s (Shorts)
on top of the chosen configs changed T1 nDCG@20 by exactly 0.0000 for RP3 and by +0.00003 for CC. Live share in the top 50 fell from 0.039 to 0.007
for RP3 and from 0.027 to 0.014 for CC, and Shorts share fell to 0. Because positives are never live (the leak in 0.1), any gain here would have been mechanical, and
there was none, because RP3's head is almost free of live streams. graph_walk's top 50 is 24% live: its zero-score ties fill the page in random order. T2
has no live or Shorts concept. **Verdict: there is no accuracy evidence either way (it cannot be tested without the leak). As page policy it has no measurable
cost and removes almost all live streams and Shorts from the top 50, so recommend it on product grounds.**

**Deciding contrast (pre-registered): best fused system vs graph_walk on T1.** The best fused system by DEV mean is
A_fused_gp_fixed: 0.0687 vs 0.0515, +33%, p=.67, CI [-0.046, +0.088]. **Null.** The CC tuned on T1 is worse at p=.046 before
Holm. The best system overall, RP3 graph alone (+58%, p=.76), is also null. **T2 analogue: fused CC vs graph_walk is +9.2%,
p<1e-5, and the guardrails pass. That is a real modelling result, but it falls below the +10% MEI.**

## 3. Release-candidate recommendation (at most 2, per the pre-registration)
No track A system clears the pre-registered bar on T1. Two candidates deserve one sealed read each:
- **RC-A1, graph alone:** the RP3 walk over stored related lists, with hub=prob, rounds=2, beta=0. Videos the graph does not reach are ordered by velocity,
  and live streams and Shorts are excluded from the recommended lane. It has the best T1 point estimate (0.082) and about 3x the recall@100. It fixes defect 1 (it scores the full
  80k corpus, not 2,000 recent rows) and defect 10 (the graph is unused today).
- **RC-A2, graph + prior:** a min-max convex combination, 0.25 graph + 0.75 velocity. It is the only configuration with a powered, Holm-significant gain
  (T2, +9.2%). On T1 it sits between graph_walk and RC-A1, and it skews the feed much newer (median age 18 days).
The verifier should read both against graph_walk, with Holm across the two contrasts. If neither clears the bar, the honest conclusion is
"graph retrieval over the full corpus, with no proven ranking lift". Online interleaving, as the research notes recommend, is the next step.

## PRODUCTION SPEC

**The method in plain terms:** "videos that YouTube's own related lists connect to the channels you follow, where a related id shared by
many videos counts for less". Each video stores up to 12 related video ids (Invidious `recommendedVideos`), and each id is a *context*.
- Seeds: mass 1 per subscribed channel, spread over that channel's videos, plus mass 1 per positive event (a watch of 50% or more or completed, a like, or more-like-this).
- A seed also belongs to the context of its own id.
- Step 1: each seed spreads its mass equally over its contexts. Each context weights the incoming mass by 1/(number of videos in it)
  and passes it on to every video in it.
- Step 2 repeats step 1 once, starting from the step 1 result, at half weight.
- Seeds themselves score 0.

The score is inspectable: each video's score is a sum over named seed videos and shared related ids, so the UI can say "because 3 videos
from channels you follow list it".
- RC-A1 orders reached videos by this score, and the remainder by velocity = log1p(views) - 1.5 x log(age_days + 1).
- RC-A2 ranks by 0.25 x minmax(graph) + 0.75 x minmax(velocity) over the eligible pool.

**Exact parameters (ref_A.PARAMS):**
- Contexts: the stored related ids (G2). video_discoveries edges are optional and off: they were chosen in 1/5 folds, and they are the first 6 entries of the same related lists.
- Walk: hub=prob, rounds=2 (second hop at weight 0.5), beta=0.
- Seeds: 1 per subscribed channel (channel-normalised), plus 1 per positive event.
- Velocity: gamma=1.5, tau=1 day (chosen in 4/5 folds).
- RC-A2 weights: 0.25 graph / 0.75 velocity, min-max over eligible videos.
- Page policy: drop live streams (liveNow or duration 0) and videos of 90 s or less from the recommended lane.
- Not candidates: seeds, videos from subscribed channels (they have their own shelves), and watched, hidden or not-interested videos.
- No content term (null on both testbeds).

**Data needed from Postgres:**
- `videos`: id, youtube_video_id, youtube_channel_id, view_count, published_at, duration_seconds, metadata->liveNow, metadata->recommendedVideos[*].videoId.
- `subscriptions`: channel ids.
- `playback_progress`: total_watch_seconds and percent_completed/completed (a seed at 50% or more or completed; also used for exclusion).
- `user_feedback`: like (seed); not_interested (exclude).
- `user_events`: more_like_this (seed); hide (exclude).
- `video_discoveries`: optional.
- `video_topics`: not used for ranking.

**Compute and latency (one server, about 80k videos with 12 related ids each, about 117k discovery edges).** Measured with ref_A.py on a synthetic
corpus of that size: 80k videos, 960k related links, 81 subscribed channels.
- Worker cycle: `Corpus.build` takes 1.5-1.8 s. It builds the CSR item x context matrix (about 1M non-zeros, about 12 MB), the degrees, the prior and the blocked mask.
  Rebuild it after each crawl or enrichment cycle and keep it in process memory.
- Per user: `UserScorer.scores` does 2-4 sparse mat-vecs plus masks, at **p95 34-42 ms**. Cache the resulting 80k-float vector (0.3 MB) per user, and invalidate it
  on a subscription or feedback change or a corpus rebuild.
- Per home load: `top_k(100)` from the cached vector takes **0.3 ms**. A cache miss (about 40 ms) is still far under the 150 ms p95 target. For comparison,
  today's list_recommended_videos takes 795-885 ms and runs 3 times per home load (diagnosis.md section 7). The 2,000-row SQL pool is no longer needed: one cached vector
  serves every lane, and reranking and diversity belong to the page track.
- Parity: ref_A.UserScorer.graph reproduces systems_A.rp3_t1(g2, prob, rounds=2, beta=0) to 1e-11 under T1's id semantics
  (parity_ref.py --t1-exact). Without that flag the correlation is 0.9998. The only difference is that production can link every video to its own id,
  while T1 knows ids only for profile videos.

**Reference implementation:** $S/alg/exp/A/ref_A.py. It is self-contained (numpy and scipy only), and its `__main__` runs a synthetic smoke test with timing.

## Limits
- T1 has 23 units, and no system scores above 0 on more than 7 of them. Every T1 verdict is underpowered, and the +58% and +33% point estimates are not evidence.
- T2 n5 and n20 were not run because their baselines are missing (0.2), so the H3 shrinkage test covers n10 only.
- Positive-event seeding in production (watches, likes) is untested. T1 seeds only subscriptions, and T2's graph is user-item, not the related-video graph.
- The diagnostic exploration (diag_*.py) looked at all DEV units before the content variants were designed, but no config was selected from it.
  Every selection was made inside nested CV, except the T2-to-T1 transfer config, which was fixed before its T1 run.
