# Pre-registration 001: deviations and operationalisations (append-only)

Logged by the harness builder, 2026-11-07/08. 001.md is unchanged. Each entry gives what was impossible or
under-specified, what was done, and why. All choices below were made from counts, field coverage or
age-only measurements on DEV, never from any candidate system's performance. Sealed units were not scored.

D1. T1 has 45 units, not 81. 36 of the 81 subscribed feeds (`feeds`) are GitHub release feeds (35) and one myYT
    feed; they have no YouTube channel and no videos. "20 SEALED, the rest DEV" therefore gives 20 sealed and
    25 DEV channels. 2 DEV channels have no positive inside the matched pool, so 23 DEV units are scored.
D2. Stratification (allowed "if strata can be computed from video counts"): terciles of per-channel video
    count; walk channels by ascending sha256(channel_id+'openfeed-prereg-001'), take each while its tercile
    holds < ceil(20/3)=7. Result 7/6/7. File: data/sealed/t1_channels.json (sha256 of the sorted list in
    data/dev/t1_dev_channels.json).
D3. "Age-matched pool" operationalised. Only 13 weeks of the corpus are dense (crawl began mid-2026);
    older weeks hold a handful of negatives, so a support-only pool makes age a near-oracle (oldest-first
    nDCG@20 0.114 vs random 0.006 on DEV). Rule fixed before any system ran: a positive or negative is
    eligible only if Invidious-enriched, dated, and in an ISO-like 7-day bin holding >= 1,000 eligible
    negatives; the unit's pool is every eligible negative in the weeks where the channel has a positive.
    No sampling. Residual age signal measured by the anti_recency diagnostic. A thinned exact-ratio pool
    exists only as a sensitivity option (T1.unit(pool="exact")).
D4. Masked beyond channel names, because they separate labels by construction (DEV measurements):
    published_at floored to the day (98% of negatives sit at 00:00:00, 11% of positives); 'summary' not
    used (85% of negatives vs 12% of positives have it), text = description only; live flag hidden
    (16% vs 0%); source_kind, discovery_source, created_at, discovered_at, metadata_fetched_at, row ids,
    channel ids hidden. Item rows are re-ordered by a salted hash.
D5. Sealed T1 profile = the 25 DEV channels only, so sealed units never enter as profile input.
D6. Crawl leak handling (001: "document how"): per hidden set, the hidden channels' videos are never
    expansion sources, and videos reachable only through them are removed (counterfactual crawl). The
    stored related lists (metadata.recommendedVideos, 12 per enriched video) are exposed with opaque
    target ids, linkable to corpus items only for profile videos. See harness README, 'Crawl graph'.
D7. 16 videos excluded everywhere as ambiguous (15 source_kind=subscription videos of channels no longer
    subscribed, 1 author-name collision).
D8. T2 label: watch_ratio > 2.0 (strict), KuaiRec paper 3.1.3 "greater than twice the duration".
D9. T2 time policy (001 is silent; brief requires time masking). 2,992 of 3,327 small-matrix items were
    uploaded after the users' first 20 events, so a global as-of cutoff leaves no evidence about most
    candidates. Item clock: crowd evidence about an item (platform counters, other users' interactions) is
    visible only for its first 3 days. Hidden users contribute nothing. Users reveal their first n
    timestamped events; untimestamped rows are always outcomes.
D10. T2 inner validation folds are capped at 60 users each (every system alike) for run time.
D11. Current ranker on T1: channel identity is masked, so watched_channel / loyalty boosts cannot fire;
     all candidates are treated as discovery rows (source masked). On T2 its TF-IDF vectors are char
     n-gram top-24 vectors (the real ASCII tokenizer yields nothing on Chinese captions). Real
     _diversify_recommendations is applied with limit 100, channel cap 2 (recommended lane).
D12. Inner folds: T1 nested 5x4 over 23 units gives 4-5 channels per inner fold.
D13. (lead, 2026-11-08) Harness bug: T2 TfidfCentroid config wc=wk=0 set X=None and crashed the baseline job, so T2 graph_walk
     and tfidf_centroid (n10) and all n5/n20 baselines were missing when tracks A and B ran. Fix: that config scores a constant
     (random tie-break). Track B re-ran the missing baselines with the same rule in exp/B/results/base; the harness copies are
     re-run by the lead. No other baseline changed.
D14. (lead, 2026-11-08) T1 leak found by track A after the 8 Nov rebuild: items.duration == 0 exactly identifies live streams
     (22.75% of eligible negatives, 0 of 985 DEV positives), so the D4 live mask was incomplete. Also by construction (D6)
     positives have no G1 out-edges (AUC of -outdegree 0.64). Handling: no release-candidate system reads duration or G1
     out-degree; any T1 accuracy effect of a live filter is labelled mechanical, not evidence. Live/Shorts filtering is decided
     as product policy, measured only on guardrails.
D15. (lead, 2026-11-08) Degraded-ranker control FAILS on T1: a 60% top-100 shuffle of graph_walk is detected in 2/20 seeds.
     T1 (23 DEV / 20 sealed units) cannot detect large differences, so T1 results are descriptive only. Decisions rest on T2,
     whose controls are re-run after D13. T1 sealed is still run once per candidate and reported, as required.
D16. (lead, 2026-11-08, written BEFORE any sealed read) Release candidates and the shipping rule.
     RC1 = track A RC-A1 (systems_A.RP3: related-list walk, hub=prob, rounds=2, beta=0; velocity orders the unreached rest).
     RC2 = track A RC-A2 (systems_A.FusedCC: min-max 0.25 graph + 0.75 velocity).  Configs fixed from DEV (exp/A/REPORT.md).
     Each is read once on T1 sealed and T2 sealed (n=5,10,20) against graph_walk (and the other pre-registered baselines),
     Holm across the two RC contrasts per testbed, by a verifier that did not tune.
     Shipping default: RC2 if its T2 sealed contrast vs graph_walk is positive with Holm p<.05 and CI>0 at n=10, and
     guardrails pass; else RC1 if its T2 sealed contrast is not significantly negative; else RC1 is still shipped as the
     retrieval fix (full corpus + graph) with the result reported as "no proven ranking lift". T1 sealed is descriptive (D15).
     Page policy (not an accuracy claim): live streams and Shorts out of the recommended lane; MMR lambda 0.7 on topic
     cosine with a 0.8^k per-channel factor (each tested alone on T2 by track B; the combination is untested).
     User-model elements from track B (graded events, weak watches dropped, watch_page_open ignored, channel penalty
     after >= 2 negatives) enter as graph seeds/exclusions; their OpenFeed effect is untested offline.
D17. (lead, 2026-11-08) seal_run.py bug: the T2 branch never set p.units (T2 keeps units in .users), so RC1's T2 sealed read
     (run d4231b4f251d) crashed with AttributeError before any unit was scored; no per-unit file was written. Fix: p.units = p.users;
     workers 12 -> 4 for memory. Added --retry-of RUN_ID, which permits one rerun only for a run of the same candidate/testbed that
     ended "failed" and wrote no per-unit result; the retry's start record names the failed run. The failed read stays in the log
     and is reported. The verifier, not the lead, runs the retry. No config changed; T1 sealed results were already logged.
