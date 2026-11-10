# Track B: user modelling under sparse history, and page re-ranking (DEV only)

S=/tmp/claude-1000/-home-peyton-app-lab-projects-openFeed/b2720c98-0a1e-4338-968f-b34b3b443505/scratchpad. Track dir `$S/alg/exp/B`.
Code: `systems_B.py` (all systems), `run_B.py` (nested CV), `run_base_B.py` (missing baselines), `run_rerank.py` (Q3),
`run_transfer.py` (H4 transfer), `run_t1_B.py` / `run_t1_fixed.py` (T1), `analyze.py` (stats). Per-unit results:
`results/t2_n{5,10,20}/*.parquet` (+ `.json` with nested-chosen configs), `results/base/`, `results/t1/`.
Tables: `results/table_main.md`, `results/contrasts_q2.csv`, `results/contrasts_q3.csv`, `results/guard_q3.csv`,
`results/contrasts_diag.csv`, `results/t1_contrasts.csv`, `results/analysis.txt`. Reference port: `ref_B.py`.

**Caveat for every T2 number:** KuaiRec is a short-video app with a different population. T2 settles modelling
questions (which construction is better). It does not give the size of any effect for OpenFeed. The label is watch_ratio > 2 (D8). In
KuaiRec that label is largely an item property (replays of short clips); see section 3.

## 0. Protocol, deviations, controls

- I followed pre-registration 001 and deviations D1-D12. Nested CV is 5x4 (inner folds capped at 60 users, D10), with a 24-config budget
  via `runner.draw` and tuning on inner folds only. Contrasts use `stats.contrast` (sign-flip 100k, paired bootstrap 95% CI).
  I applied Holm across **all** Q2 contrasts of all three tags at once (40 tests), which is more conservative than per tag. Holm is also
  applied across the Q3 contrasts, and BH across guardrails. A result "counts" when Holm p < .05, the CI is above 0 and no guardrail fails (>10% relative worse, BH q < .05).
- **My deviations.** Each was made before I read the results it affects, unless stated otherwise:
  - **B1. Structure screening.** Before any nested run, I screened about 45 configurations on 150 users of outer fold 0 at n=10.
    Each was fit on that fold's training view, with no tuning. This chose the component structure: popularity prior, ItemKNN with
    watch_ratio >= 2 and shrink 100, the naive-Bayes lift CF, and z-score fusion. Those 150 users are also DEV outer units,
    so the structure (not the parameters) carries a small selection bias. Sealed users were never touched.
  - **B2. Missing baselines.** The lead's detached baseline job died at T2 `tfidf_centroid` (harness bug, section 7). As a result,
    `graph_walk` and `tfidf_centroid` at n=10 and every baseline at n=5 and n=20 did not exist. I re-ran them with the
    unchanged classes, seeds and budget (`run_base_B.py`, output in `results/base/`). The only change: the
    `tfidf_centroid` configs with wc=wk=0 score a constant instead of crashing. I did not re-run `current_real_pool` and
    `current_full` at n=5/20. At n=10 they score 0.042 and 0.043, far below popularity, and current_full took 90 minutes.
  - **B3. Speed and memory.** These changes do not alter results. Item-item matrices are cached on disk (memory-mapped), keyed by a fingerprint
    of the training evidence. My scripts cap the harness's `train_view` memo at 3 views (no harness edit).
- **Controls.**
  - The harness's T2 controls never ran: `controls_t2` crashed on the missing graph_walk.json.
  - For my best system I ran three checks myself:
    - Negative control (labels shuffled within each unit): nDCG@20 0.0473 against an expected random 0.0470. Pass.
    - A/A (tie-break seed 0 against seed 1): identical per unit. Pass.
    - A frozen-config replay at n=10 reproduces the nested result exactly (0.42899).
  - I did not re-run the positive and leak controls; they are harness-level.
  - T1 controls (lead's log): negative, positive, A/A and leak pass. The **degraded-ranker control fails**: a -39% degradation is detected in only 10% of seeds. T1 therefore cannot detect even large harm, so treat every T1 verdict below as "no evidence", not "no effect".

## 1. Baselines (best tuned per tag)

| tag | random | recency | popularity | velocity | tfidf_centroid | graph_walk | best |
|---|---|---|---|---|---|---|---|
| n5 | 0.049 | 0.065 | **0.294** | 0.049 | 0.056 | 0.281 | popularity |
| n10 | 0.047 | 0.065 | 0.291 | 0.049 | 0.057 | **0.326** | graph_walk |
| n20 | 0.049 | 0.066 | 0.288 | 0.049 | 0.064 | **0.350** | graph_walk |

Popularity is KuaiRec's complete_play rate over each item's first 3 days, chosen in every fold. graph_walk on T2 is user-based CF over other
users' events with watch_ratio >= thr.

## 2. Systems

Every T2 system scores score = z(prior) + n/(n+k) * [nbw * z(NB lift) + beta * z(similarity profile)]. The prior is the
pre-registered popularity formula (stat c, pseudo-count 100), which was the tuned baseline's choice. k = 0 means no shrinkage.
- **NB lift CF** (`_nb`): each revealed event j falls in an outcome bin b: low (wr < 0.5), mid (0.5-2), high (> 2), or "seen" (any outcome).
  The lift is log P(i satisfied | j in bin b, i seen) - log P(i satisfied | i seen). It is estimated from other users' events under the item
  clock (pseudo-count 100) and summed over events. This is outcome-aware item-item CF; the bins are the graded label.
- **Similarity profile**: ItemKNN cosine (other users' events with watch_ratio >= 2, shrink 100), EASE (lambda tuned), or content
  (char TF-IDF + category). Aggregation is a centroid, max-sim over all events, or 2-4 medoids (average-linkage clusters).
- `c_*` systems: same structure, with the crowd satisfaction rate (other users, first 3 days) as the prior. These are T2 only, because OpenFeed has no crowd.

The main results table gives nDCG@20, recall@100 and MRR with bootstrap 95% CIs. Guardrails are computed over the top 50. Latency is the per-unit p95 in ms for
about 3,300 candidates. The full table is `results/table_main.md`.

| tag | system | nDCG@20 | recall@100 | MRR | ILS | Gini | calib KL | med. age | p95 ms |
|---|---|---|---|---|---|---|---|---|---|
| n5 | popularity (best) | 0.294 [0.285, 0.302] | 0.187 [0.180, 0.195] | 0.733 [0.710, 0.756] | 0.0226 | 0.141 | 1.813 | 47.4 | 0.6 |
| n5 | b_fused_k | 0.392 [0.382, 0.402] | 0.239 [0.229, 0.250] | 0.743 [0.723, 0.763] | 0.0213 | 0.062 | 1.844 | 48.8 | 4.8 |
| n5 | b_fused_k + MMR 0.7 | 0.399 [0.389, 0.409] | 0.239 [0.229, 0.250] | 0.744 [0.724, 0.764] | 0.0182 | 0.048 | 1.760 | 48.9 | 2.7 |
| n10 | graph_walk (best) | 0.326 [0.316, 0.336] | 0.162 [0.156, 0.169] | 0.683 [0.662, 0.705] | 0.0200 | 0.152 | 1.736 | 42.5 | 4.2 |
| n10 | current_real_pool | 0.042 | 0.033 | 0.125 | 0.268 | 0.089 | 1.307 | 30.9 | 13.5 |
| n10 | b_fused_k | 0.429 [0.419, 0.439] | 0.257 [0.245, 0.268] | 0.754 [0.734, 0.773] | 0.0205 | 0.050 | 1.557 | 49.6 | 8.3 |
| n10 | b_fused_k + MMR 0.7 | 0.443 [0.433, 0.453] | 0.257 [0.245, 0.268] | 0.756 [0.736, 0.775] | 0.0179 | 0.037 | 1.451 | 49.3 | 3.8 |
| n10 | c_prior (crowd, non-personal) | 0.636 [0.625, 0.647] | 0.337 [0.323, 0.351] | 0.845 [0.829, 0.862] | 0.0188 | 0.074 | 1.692 | 41.5 | 1.0 |
| n20 | graph_walk (best) | 0.350 [0.341, 0.360] | 0.168 [0.162, 0.173] | 0.727 [0.707, 0.747] | 0.0201 | 0.160 | 1.545 | 39.9 | 2.7 |
| n20 | b_fused_k | 0.462 [0.453, 0.473] | 0.271 [0.259, 0.283] | 0.787 [0.768, 0.805] | 0.0202 | 0.040 | 1.323 | 49.8 | 4.0 |
| n20 | b_fused_k + MMR 0.7 | 0.475 [0.465, 0.485] | 0.271 [0.259, 0.283] | 0.785 [0.766, 0.804] | 0.0178 | 0.030 | 1.182 | 49.3 | 3.8 |

`b_fused_k` is the best user model. It chose the same config in all 15 outer folds: nbw 1.0, beta 0.1 (ItemKNN max-sim), k 5,
and NB bins mid + high (weak watches dropped). Live and Shorts shares are undefined on T2. Its latency is below the current ranker's
13.5 ms p95 on the same units.

## 3. Q2 verdicts (user model). Contrast table: `results/contrasts_q2.csv`

Values are reference → new nDCG@20, diff [95% CI], relative change, and Holm-adjusted p (40 tests).

**Overall, best user model vs best baseline: SUPPORTED on T2, but mostly not personal.**
b_fused_k against the best baseline:

| tag | baseline → b_fused_k | diff [95% CI] | relative |
|---|---|---|---|
| n5 | 0.294 → 0.392 | +0.098 [+0.090, +0.105] | +33.3% |
| n10 | 0.326 → 0.429 | +0.103 [+0.094, +0.113] | +31.7% |
| n20 | 0.350 → 0.462 | +0.112 [+0.104, +0.121] | +32.0% |

Holm p = 0.0004 in each case and no guardrail fails (Gini falls 56-75%). Two controls limit what this means:
- **Shuffled-profile control.** This attaches the user's outcomes to random items and runs the same model.
  - b_nb_2 real against shuffled: +9.6% / +10.2% / +6.2% (n5/10/20), Holm p 0.0004.
  - b_nb_2 gains +0.064 / +0.112 / +0.158 over popularity. The shuffled profile already gets +0.033 / +0.074 / +0.132 of that.
  - So half to four fifths of the "user model" gain is item information the CF term carries regardless of who the user is. The genuinely personal part is +0.026 to +0.037 nDCG.
- **Crowd prior.** This prior is non-personal: other users' satisfaction rate in the item's first 3 days.
  - It reaches 0.636 at n=10. That is +95% over the best pre-registered baseline and +48% over the best user model.
  - With it in place, nothing personal adds anything: NB lift -0.0002 [-0.0007, +0.0004], ItemKNN -0.0005 [-0.0014, +0.0007], NB real against shuffled -0.0003.
  - In KuaiRec at n <= 20, item quality dominates, and 5-20 events cannot improve on a good item prior.
  - For OpenFeed this means the user model is a secondary lever. The prior (subscriptions, plus any quality signal) carries most of the ranking.

**H1 graded label (signals F1-F4, H1). SUPPORTED in the outcome-conditioned form; NULL as similarity weighting.**
- Graded outcome bins against binary "watched" (b_nb_3 vs b_nb_seen): +9.1% (n5), +15.8% (n10), +28.2% (n20); Holm 0.0004.
- Binary "satisfied only" against binary "watched" (b_nb_pos vs b_nb_seen, n10): -9.7% [-0.034, -0.027]. Discarding the
  unsatisfied events loses information.
- Weighting a similarity profile by watch ratio: wr gives +0.6% [-0.0006, +0.0040] and min(r,1)*log1p(s) gives -0.1%; both have Holm p 1.0.
  What helps is treating each outcome level as different evidence about other items, not scaling by the ratio.

**H2 weak negatives (signals F5, H2). SUPPORTED: drop them. Do not use them as positives or as learned evidence.**
- Graded with the low bin dropped, against graded with it kept (b_nb_2 vs b_nb_3): +6.3% / +11.5% / +12.3%, Holm 0.0004.
- In ItemKNN, dropping weak events against counting them as positives: +0.2% [-0.0002, +0.0014], null.
- Used as negatives (Rocchio, H3 below), they are practically null.

**H3 Rocchio negatives g=0.2 and author penalty (signals F7/F9, H3, H5; reranking H8). NULL on T2 / UNTESTABLE.**
- Rocchio g=0.2 on weak negatives, against simply dropping them: +0.0008 [+0.0003, +0.0014] (+0.3%), Holm p 0.022.
  - This is detectable but 30 times below the minimum effect of interest (+10%), so it is practically null.
  - T2 has no explicit negatives (hide / not-interested), so Rocchio on explicit negatives is untested here.
- The author penalty after >= 2 negatives fires for 0, 4 and 16 of 988 users at n=5/10/20, because two weak negatives on one author
  are rare. Results are identical to the system without it, so it is untestable on T2. On T1 the channel is masked, so it is untestable there too.

**H4 shrinkage of prior + n/(n+k) personal (signals F10/F11, H4, H7). WEAKLY SUPPORTED as a form. k=5 on T2, k=20 for OpenFeed.**
- Nested CV chose k=5 (the smallest value offered) with the same weights in all 15 outer folds.
- The tuned personal weight of b_nb_2 grows with n: nbw 0.5 (n5), 0.75 (n10), 1.0 (n20, the grid edge). This matches the n/(n+k) shape.
- Clean transfer test: I froze b_fused_k's n=10 config and a constant-weight twin that is identical at n=10, then scored both at n=5 and n=20.
  - n5: shrinkage against constant +0.0011 [-0.0013, +0.0035] (+0.3%, not significant). The twin has 13% lower Gini.
  - n20: +0.0092 [+0.0074, +0.0110] (+2.0%, Holm 0.0004).
  - The direction is right and the size is small.
- b_fused_k against b_fused (+1.8% at n10, +5.5% at n20) is confounded. b_fused's weight grid stopped at 0.5, so this mostly shows
  that a larger personal weight helps at larger n. It is not counted as evidence for the form.
- Sensitivity, with nbw 1.0 and beta 0.1 frozen:
  - k=10 gives 0.376 / 0.422 / 0.453 (n5/10/20).
  - k=20 gives 0.357 / 0.397 / 0.438. So k=20 costs 5-9% against k=5 on T2 and still beats the best baseline by 21-27%.
- T1 (section 5) pulls the other way. With the real user's 3 to 6 events, k=5 lowered subscription-holdout nDCG by 30% (not significant) and k=20 by 2.8%. Production therefore uses k=20 (see the spec).

**H5 item-item CF fused with the prior (ItemKNN, EASE). MIXED.**
- NB-lift CF (outcome-aware) plus popularity, against the best baseline: +21.8% / +23.5% / +27.4%, Holm 0.0004.
- ItemKNN max-sim plus popularity alone is below the user-based baseline:
  - n5: +3.5% against popularity.
  - n10: -5.9% [-0.027, -0.012] against graph_walk.
  - n20: -9.1% against graph_walk.
- EASE (lambda chosen between 100 and 2000) at n10: -3.0% [-0.017, -0.003], Holm 0.076.
- Adding ItemKNN max-sim to the NB term (b_fused vs b_nb_2) is a small, n-dependent effect:
  - n5: +9.6%.
  - n10: +4.8%, with the Gini guardrail +25% relative on a tiny base.
  - n20: -1.8%.
- A content personal term (char TF-IDF + category) on top of popularity gives -0.8% [-0.004, -0.001]: null to slightly harmful
  on Chinese short-video captions. This does not transfer to English YouTube text in either direction.

**H6 multi-exemplar profiles (signals F13-F15, H6). Max-sim SUPPORTED; medoids REJECTED.**
On ItemKNN at n=10:
- Max-sim over all events against a centroid: +2.8% [+0.006, +0.010], Holm 0.0004.
- 2-4 medoids against a centroid: -1.6% [-0.007, -0.003]. K=2 was chosen in 4 of 5 folds.

With 20 or fewer events, keeping every event as its own exemplar beats compressing them into medoids. PinnerSage's gains came from long histories.

**H8 time decay (signals F12, H8). NULL.** A half-life of 5 or 10 events against none: -0.0001 [-0.0016, +0.0014]. Koren's finding of "no decay
with scarce data" holds. KuaiRec's reveal window is short, so this only tests within-session order.

Some systems were run at n=10 only: H1/H2 in ItemKNN, H3, H6, H8, EASE and content. All their n=10 effects are below 3%.

## 4. Q3 page re-ranking on top of b_fused_k (`results/contrasts_q3.csv`, `results/guard_q3.csv`)

Re-ranker parameters were fixed by each hypothesis, with no tuning. Each re-ranker works greedily over the top 200 and fills the first 50 slots. The reference is the same
scorer replayed without re-ranking. The table gives the relative nDCG@20 change at n5 / n10 / n20 (Holm across the 18 Q3 tests). It also gives the relative guardrail
change at n=10 for ILS, author Gini and calibration KL, where negative is better.

| re-ranker | nDCG@20 n5 / n10 / n20 | Holm p | ILS | Gini | calib KL | verdict |
|---|---|---|---|---|---|---|
| MMR lambda 0.7 (content cosine) | +1.8% / +3.2% / +2.8% | 0.0002 all | -12.7% | -26.2% | -6.8% | **adopt**: no accuracy cost, all guardrails better |
| MMR lambda 0.85 | +1.4% / +1.8% / +1.8% | 0.0002 all | -5.9% | -13.9% | -3.5% | weaker version of the above |
| smooth author 0.8^k | -0.0% / +0.3% / +0.1% | 1.0 / 0.03 / 0.71 | +1.3% | -50.1% | -0.6% | neutral accuracy, halves author concentration |
| hard cap 2 per author | +0.0% / +0.1% / +0.0% | 1.0 / 0.005 / 0.71 | +0.4% | -15.7% | -0.3% | dominated by 0.8^k on Gini |
| calibration lambda 0.3 (Steck) | -9.2% / -7.9% / -8.6% | 0.0002 | +0.3% | -0.3% | -14.9% | reject: costs 8-9% nDCG, MRR -22% |
| calibration lambda 0.5 | -13.9% / -12.9% / -13.0% | 0.0002 | +0.3% | +1.1% | -28.9% | reject: material accuracy loss |

- The smooth-against-cap contrast (reranking H3) favours the smooth penalty: same accuracy, with Gini -50% against -16%.
- No re-ranker failed a guardrail.
- MMR's small accuracy *gain* plausibly comes from not stacking near-duplicates that share an outcome. On OpenFeed, treat it as "free diversity", not as an accuracy lever.
- MMR and 0.8^k together were not tested.

## 5. T1 check with the real user (`results/t1/`, `results/t1_contrasts.csv`; 23 DEV channels)

`b_t1` is built as follows:
- **Prior**: related-list co-citation from visible subscribed videos (graph_walk g2 form, fixed).
- **Personal term**: the real user's visible events, graded as in production. In fold 0 there are 3 positives (two real playbacks and one mark_watched) and 4 negatives (the 4 hidden / not-interested videos). The term combines co-citation from those events with content max-sim, shrunk by n/(n+k).
- **Rocchio**: weight g on the negatives.
- **Reference**: the same prior alone.

| system | nDCG@20 | diff vs prior [95% CI] | rel | p | units changed |
|---|---|---|---|---|---|
| prior only (b_t1_prior) | 0.0551 | | | | |
| graph_walk (re-run, = old 0.0515, pools identical) | 0.0515 | -0.0035 [-0.0089, -0.0001] | -6.4% | 0.13 | 4 |
| b_t1 nested (beta chosen 0 in 3 of 5 folds) | 0.0459 | -0.0092 [-0.0238, 0.0000] | -16.7% | 0.50 | 2 |
| production form, k=5, g=0.2 (frozen) | 0.0385 | -0.0166 [-0.0372, +0.0007] | -30.1% | 0.11 | 10 |
| personal k=5 without Rocchio | 0.0364 | -0.0187 [-0.0434, +0.0008] | -34.0% | 0.12 | 10 |
| Rocchio only g=0.2 | 0.0572 | +0.0022 [-0.0003, +0.0067] | +4.0% | 0.50 | 5 |
| k=20, g=0.2 (frozen) | 0.0535 | -0.0015 [-0.0034, -0.0002] | -2.8% | 0.06 | 5 |
| k=50, g=0.2 (frozen) | 0.0546 | -0.0004 [-0.0009, -0.0001] | -0.8% | 0.13 | 4 |

- Nothing is significant (Holm >= 0.44), and T1 cannot detect a -39% degradation.
- The point estimates say a strong personal term (k=5) pulls the ranking away from the held-out subscriptions, which is what T1 measures. At k=20 the cost is within 3%.
- Rocchio on the 4 real negatives did not hurt.
- T1 guardrails for the k=5 form: ILS -2%, Gini -2%, calib KL -22%, and live share 0.209 against 0.243.
- p95 latency is 56 ms per unit, against 8 ms for the prior alone. It is dominated by sparse TF-IDF products on candidate pools of about 40k; see the budget below for the full-corpus path.

## 6. What T2 decided, in brief

With 5-20 events, the label-aware part of the user model matters:
- Grade each event by its outcome and drop weak watches: +12-28% over binary.
- Keep every event as an exemplar and score by max-sim: +3% over a centroid. Medoids do worse.
- Shrink the personal part toward the prior with n/(n+k): a small gain, +2% at n=20.
- Diversify the page with MMR lambda 0.7: no accuracy cost.

Negatives from weak watches, time decay, watch-ratio weighting of similarities and content profiles do nothing.

The personal part is small next to item quality. A non-personal crowd prior beats every user model by about 50%, and with it in
place no personal signal adds anything.

## 7. Harness bug (evidence)

The bug is in TfidfCentroid on T2 in `systems/baselines.py`:
- `space_t2` contains wc=0.0 with wk=0.0. For that case `fit` sets `self.X = None` and then calls `self.X.multiply`.
- The result is `AttributeError: 'NoneType' object has no attribute 'multiply'` (`results/run_t2_all.log`).

Consequences:
- The lead's chain stopped after `current_full` at n=10.
- There are no T2 graph_walk or tfidf_centroid results at n=10, and no T2 baselines at n=5 or n=20.
- `controls.py t2` crashed on the missing `graph_walk.json` (`results/controls_t2.log`), so **the T2 controls have not run**.

I did not edit the harness; my workaround is deviation B2. Lead action: fix the
wc=wk=0 case so that it scores a constant, then re-run `controls.py t2`.

## PRODUCTION SPEC

**Method in plain terms.**
1. Score every candidate with a subscription prior.
2. Add a personal term from the user's own graded events, shrunk toward the prior by n/(n+20).
3. Subtract a penalty for similarity to explicitly rejected videos.
4. MMR-diversify the first 50 slots.

Weak watches count for nothing. Every term is a named, inspectable number.

```
score(i) = z(prior_i) + n/(n+K) * [ W_CF * z(lift_i) + W_SIM * z(maxsim_i) ]
           - G_NEG * z(max cosine to explicit negatives) - CHANNEL_PENALTY * [channel has >= 2 negatives]
page     = MMR(score, lambda = 0.7, similarity = topic cosine), top 200 -> first 50 slots; rest by score
```

**Parameters.**

| parameter | value | basis |
|---|---|---|
| K | 20 | T2 chose 5; T1 point estimates favour 20. Adjustable. |
| W_CF | 1.0 | T2 nested CV, every fold |
| W_SIM | 0.1 | T2 nested CV, every fold |
| G_NEG | 0.2 | Safety prior; explicit negatives untested |
| CHANNEL_MIN_NEG | 2 | Untested |
| CHANNEL_PENALTY | 1.0 z | Untested, hand-set |
| WEAK_RATIO | 0.5 | T2 bins |
| positive weights | mid 0.5 / high 1.0 / explicit 1.0 | Hand-set ordering |
| MMR_LAMBDA | 0.7 | Q3 |
| MMR window | top 200, 50 slots | Q3 |

- **Optional:** a smooth channel factor 0.8^k after MMR for author spread. Tested alone, it gave Gini -50% with neutral accuracy. It was not tested in combination with MMR.
- **Do not add:** calibration re-ranking, time decay, medoid profiles, watch-ratio similarity weights, or weak-watch negatives.

**Signal mapping (OpenFeed tables).**

| element | OpenFeed source | rule | evidence |
|---|---|---|---|
| graded positive, weak watch dropped | `playback_progress` (percent_completed, total_watch_seconds, completed) + `videos.duration` | ratio r = percent_completed (else seconds / duration). r < 0.5 and not a real completion: dropped (not positive, not negative). 0.5 <= r < 0.9: mid, weight 0.5. r >= 0.9 or completed with seconds > 0: high, 1.0. completed with 0 seconds (mark_watched): mid 0.5 | H1 +9-28%, H2 +6-12% (T2 bins; the r >= 0.9 cut is a mapping, T2's high bin was replays) |
| explicit positive | `user_feedback.reaction = like`; `user_events.more_like_this` | weight 1.0 (as high) | no T2 analogue |
| not a positive | `user_events.watch_page_open` without playback | ignored | binary "seen" evidence -23% vs graded (n10) |
| explicit negative | `user_feedback.not_interested`; `user_events.hide` | exact video excluded; Rocchio g=0.2 on topic cosine; counts toward the channel rule | Rocchio null on T2 weak negatives, +4% (ns) on T1 real negatives |
| channel penalty | negatives grouped by `videos.channel_id` | -1.0 z only when >= 2 independent negatives on the channel | untestable (fires for 4/988 T2 users) |
| prior | `subscriptions` -> subscribed channels' `videos` | co-citation of the candidate in related lists of subscribed videos (`video_discoveries` source->discovered plus stored related ids), idf-weighted, cosine-normalised | T1 best baseline form (graph_walk g2 0.0515; fixed variant 0.0551) |
| item-item similarity for lift / max-sim | `video_discoveries` + related lists; `video_topics` | lift = weighted mean related-feature cosine to positive events; max-sim = max over events of w_j * max(related cosine, topic cosine) | stands in for T2's crowd CF (OpenFeed has no other users); the T2 numbers do not transfer to this substitute |
| n | count of positive events after grading | n/(n+20) | H4 |
| MMR similarity | `video_topics` TF-IDF vectors | cosine | Q3 |

**What will not transfer.**
- The biggest T2 component, the outcome-aware lift from other users' co-watching, has no OpenFeed data source.
- The related-video graph is only a proxy: YouTube builds it from its own users, and it carries no outcome bins.
- Expect the personal term in OpenFeed to be weaker than on T2.
- The crowd-prior result implies the strongest lever for OpenFeed is a better item prior (subscriptions, plus a quality signal such as like/view rates). That is Track A/C territory.

**Data needed per request.**
- Candidate rows from `videos`: id, channel_id, duration, published_at.
- Related-feature CSR, built from `video_discoveries` and the stored related ids.
- Topic CSR from `video_topics`.
- The user's `playback_progress`, `user_feedback`, `user_events` and `subscriptions`.

The caller excludes already-watched videos from the candidates.

**Compute budget (about 82k videos, one server).**
- **Precompute and cache.** Rebuild when the crawl or the subscriptions change.
  - Related CSR: about 82k x 400k, under 1M nnz, about 10 MB, built in 0.1 s.
  - Topic CSR: about 80k x 70k, about 0.8M nnz, about 10 MB.
  - Subscription prior vector: one sparse product.
- **Per request.** One sparse product per term over all candidates, plus MMR on a 200x200 matrix.
- **Measured.**
  - `ref_B.py` scores all 82k synthetic candidates and builds the page in 151 ms median (pure numpy/scipy, this host). One `list_recommended_videos` call takes 795-885 ms today.
  - On T2 the per-user cost was 3-8 ms p95 for 3,300 candidates, below the current ranker's 13.5 ms.
- **Memory.** Under 100 MB resident. No training step.

**Reference implementation.** `$S/alg/exp/B/ref_B.py` provides `build_events`, `related_matrix`, `subscription_prior`, `score` and
`mmr_page`. Running `python -I ref_B.py` runs a self-test. The file encodes the parameters above and the event mapping exactly.

**Before shipping.**
- Log feed impressions with their score components (reranking.md section 6). No offline test here measures the substitute item-item term on OpenFeed's own signals.
- Expose K, G_NEG and MMR_LAMBDA as settings.
