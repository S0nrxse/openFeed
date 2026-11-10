# OpenFeed ranker: forensic diagnosis (measured, dev copy of production, 2026-11-06)

Code: $S/wt/alg/app. Real user (R). Empty user = "diag-empty-user-0000" (N). Scripts: $S/alg/diag/ (run_all.sh,
raw output in dNN.out). S=/tmp/claude-1000/-home-peyton-app-lab-projects-openFeed/b2720c98-0a1e-4338-968f-b34b3b443505/scratchpad
Corpus is 82,041 videos (brief said 81k): 80,104 discovery, 1,937 subscription; 81 subscriptions.

## 0. Brief corrections
- Real user has 4 hide events + 4 not_interested rows (the same 4 videos), 5 mark_watched, 7 watch_page_open, 10 playback rows
  (2,555 s total), 0 likes, 0 takeout rows. "15 hides / 8 NI" is not reproducible for R. 'local' (excluded) has hide 14, NI 1, dislike 7.
- Only 4 of R's 10 playback rows have total_watch_seconds > 0; 5 completed rows have 0 seconds (mark_watched).

## 1. User profile (R) and suspected problems
- profile_vector: 45 terms, built from 7 videos (watched_topic_vectors needs seconds>0 or completed). Top 10: blueprint .34, here's .31,
  scale .31, i'd .30, wanted .26, carpenter .26, sabrina .24, sweeney .23, feminism .23, service .22. Generic words ("here's", "i'd",
  "wanted", "real", "great") are not in STOPWORDS; one 2,045 s [channel] video supplies 4 of the top 5 terms.
- interest_exemplars: 7 vectors, 4 to 9 terms each (title fragments redacted).
- watch_time_profile: window is 60 days, so the 2,045 s video (63 days old) is excluded. Left: 393.7 decayed seconds, confidence
  0.109, 2 channels (91% / 9%), 8 topics = raw YouTube tags/channel names ("modern wisdom", "chriswillx"), the junk topics.py's own
  docstring condemns. Fires on 4 of top 100 (watched_channel) and 13 (watched_topic): 1.8% of score mass.
- takeout_profile: empty (0 channels, confidence 0). N: every profile is empty (0 terms, 0 exemplars, confidence 0).
- CONFIRMED: with thin history the feed is near-recency. N top 100: base (recency bucket + 0.4 discovery) is 52% of |score|, token
  overlap with subscriptions 28%, discovery_balance 19%, nothing personal fires; score range 2.42..4.90, sd 0.71.
- CONFIRMED: subscriptions do not seed the semantic profile (code reads only playback + likes). The only subscription use is
  subscription_topic_affinity (token counts from the 500 newest subscription videos). Those cover 39 of 81 channels; top tokens are
  [four channel names, 60-61 each]: channel names, not topics. Fires on 91% (R) / 100% (N) of top 100.
- CONFIRMED evaluation.py is dead: R has 10 history rows < holdout 20 -> "Not enough watch history", empty result. holdout=5 forces
  recall@10/20/50 = 0, MRR 0, all 5 held-out "filtered from pool" by design (watched excluded). It cannot register any change.
- CONFIRMED unused tables: recommendation_runs, recommendation_candidates, feed_impressions, interactions all 0 rows; code touches them
  only in a COUNT(*) status query. No impression logging exists, so no exposure data for any future counterfactual evaluation.

## 2. Score composition and ordering (top 100, mean abs contribution; % of total |score|)
R, recommended lane: base 2.46 (36.7%), interest_exemplar 1.28 (19.1%, fires 26), subscription_topic_affinity 1.12 (16.7%, fires 91),
discovery_balance 0.98 (14.6%, fires 89), semantic_affinity 0.67 (10.0%, fires 44), watched_topic 1.3%, token_interest_match 0.8%,
watched_channel 0.5%, unknown_quality 0.2%. Personalised signals total ~32%.
R, all lane: subscription_source 6.0 (28.4%, 75 fire), base 5.7 (26.9%), subscribed_channel 3.8 (18.0%), interest_exemplar 10.9%,
subscription_topic_affinity 7.8%, semantic 6.0%. Flat subscription bonuses = 46% of score; 76 of 100 are subscribed-channel videos.
N, recommended: only base, subscription_topic_affinity, discovery_balance, unknown_quality fire. N, all: +subscription flat 54%.
- Never fire for R (recommended): subscription_source, subscribed_channel (zeroed by design), watched_channel_loyalty (needs >=2
  completions in one channel), ungrounded_discovery_penalty, preferred_topic, muted_topic, takeout_*, quick_abandonment,
  partial_watch_penalty (watched videos are excluded in SQL, so these two can never fire), undated_discovery_penalty (undated videos never
  reach the scorer). Fires on everything: base, subscription_topic_affinity, discovery_balance (89%).
- Across the whole 2000 pool (R): semantic_affinity fires on 84 (4.2%), interest_exemplar 26, watched_channel 4, ungrounded penalty 766.
- Spearman(final rank order, recency): R recommended -0.24, N recommended -0.04, R all -0.81, N all -0.18. Ranks 1-20 of R/all have
  median age 63 days (ranks 61-100: 0.6 days). Score is not recency-ordered inside the page; recency acts as a hard pool filter:
  100/100 of top-100 recommended are <=5 days old (70 are <3 d). Overlap of the top 100 with the 100 newest pool items: 8 (R), pool
  score-vs-recency rho +0.27 (R), +0.30 (N).
- Top 100 recommended (R): 82 distinct channels (max 3), 89 discovery / 11 subscription (25 from subscribed channels), 7 non-Latin
  titles, 12 with no topic_vector, 14 live streams, 18 sourced from "trending", 10 with no duration. N: 81 channels, 0 non-Latin, 7 videos <=90 s.
- Top 100 all (R): 47 channels, 76 subscribed, 25 with no vector, 10 videos <=90 s. Shorts not filtered in the lane (the home page drops them).

## 3. Candidate pool (what the Python scorer sees)
- Recommended lane: 2,000 of 82,041 (2.4%) rows. Selection is the SQL "crude score": recency bucket (+2.4 <3 d, +1.4 <14 d, +0.5 <60 d)
  + 0.4 flat for discovery + feedback terms, so it is recency order. Pool ages: 754 <3 d, 1,246 in 3-14 d (21% of that bucket), 0 older.
  Oldest item 5.1 days; median 3.1. Composition 1,989 discovery / 11 subscription, 1,135 channels, 1,763 with vectors.
- Corpus by age (corpus / in pool): <3 d 805/754; 3-14 d 5,897/1,246; 14-60 d 29,181/0; 60 d-1 y 41,289/0; >1 y 4,869/0.
  76,000+ videos (93%) can never be recommended, whatever their relevance. The 15,483 undated videos (18.9%) get no recency bucket: 0 reach the pool.
- Only recency acts: no diversity, quality, popularity, topic, or per-channel stage precedes the 2000 cut. N gets the identical pool.
- Of 4,292 videos in subscribed channels, 4,262 (99.3%) are never evaluated in the recommended lane; 29 of 45 subscribed channels with stored
  videos have none in the pool. (Median age of unevaluated: 131 d, min 3.1 d.)
- By topic (R, excluding the 14 touched videos): videos with max(profile, exemplar) cosine >0.15: 1,320, only 26 in pool; >0.3: 143, only 5;
  >0.5: 15, only 1. Best unevaluated matches: 0.89 (273 d, [channel]), 0.88 (57 d), 0.72/0.68/0.65 ([channel], 68-133 d). Max cosine in pool 0.59.
- All lane: pool is 1,914 subscription + 86 discovery (subscription_weight +6 decides the cut), 68 channels, ages up to 144 d.

## 4. Corpus quality (82,041 videos)
- topic_vector: 80,652 present, 1,389 empty (1.7%); of the empty, 270 (19.4%) non-Latin-dominant title, 1,119 never enriched. Size: mean 9.7,
  median 8; <=5 terms 17,948 (22%); <=10 56,519 (69%); full 24 terms 6,867 (8.5%).
- ASCII-only TERM_RE: 1,852 titles (2.3%) are non-Latin-dominant (Hangul 587, CJK 334, Cyrillic 244, Arabic 175, kana 260, Thai 35). 1,582 of
  them still have a vector, but built only from Latin tokens in tags/channel/description; title text contributes nothing. Impact on
  vector presence is small (<0.4% of corpus); impact on those videos' vector quality is total.
- Non-English: 43.4% of titles contain no English function word (heuristic; includes short titles and other Latin languages).
- Shorts: <=60 s 255 (0.3%), <=90 s 457 (0.6%), /shorts/ URL 1. Shorts are essentially not in the corpus. Live: metadata liveNow=true on 12,673
  (15.4%); 8,898 come from "trending" (13,054 trending rows = 15.9% of corpus, median age 47 d). liveNow=true among videos <14 d old: 1,438 of 6,422 (22%).
  Flag is a snapshot at enrichment and may be stale, but 14 of R's top 100 carry it. Duration unknown 15.4% (12,617 of those are live).
- Views (89.3% known): p10 957, p25 5,270, median 36,607, p75 231k, p90 979k, p99 9.75M; 7,508 under 1,000. Age: median 57 d, p95 509 d.
- like_count 87.9% present (86.2% > 0); dislike_count 40 rows (0.05%). description 84.8%, tags 52.6%.
- Top terms by df*idf in stored vectors: live 9,547 docs, news 3,550, trump 2,905, show, day, football, now, game, week, breaking, world,
  every, college, first, time, will, sports, nfl, today, stream, season, fox, iran, real, life, join, war, minecraft, daily, back.
  Boilerplate (subscribe/instagram/official/channel...) is 1.04% of mass; the generic head ("live", "now", "every", "will", "time",
  "back", "real") is the problem, not link boilerplate. "live" alone has the largest raw mass.
- Channel names: the video's own channel tokens hold 14.7% of vector mass, 12.8% of squared norm; 2,886 videos (3.6%) are >50% channel
  name. Mean cosine of same-channel pairs 0.274 vs 0.002 for random pairs, so cosine partly measures "same uploader".

## 5. video_discoveries graph
- 117,459 edges, all source=invidious_related. Every expanded node has exactly 6 out-edges (19,571 of 19,576; 3 with 8): the crawl keeps
  the first 6 related. Only 19,576 videos (23.9%) have out-edges; 67,024 (81.7%) have in-edges; 7,254 isolated. In-degree: mean 1.75,
  median 1, 50,863 targets (76%) have in-degree 1, p99 12, max 208.
- Weak components 141, giant one 73,698 nodes (98.5% of nodes with edges). Strong components 71,298, largest 76: directed walks die fast.
- Subscribed-channel videos: 4,292; 1,763 (41.1%) have out-edges (10,584 edges, 6,642 distinct targets, 2,371 land on other subscribed videos).
  1-hop targets that are dead ends: 5,625 (84.7%). 2-hop cumulative reach 8,361 videos = 10.2% of corpus. 1,705 targets are reached from >=2
  subscribed videos (max 21), a usable co-citation signal.
- Same-channel edges 18,249 (15.5%). Mean edge age 64.9 d (median 59, max 140.5); 6,516 edges <7 d. Targets <14 d old: 9.3%.
- Signal check: mean topic cosine of edge endpoints 0.098 (cross-channel 0.066) vs 0.003 random pairs (22x to 33x lift).
- Usable? As a candidate generator and co-citation score from subscribed/liked videos, yes (98 one-hop targets sit in the pool, only 8 reach
  the top 100). As a long random walk, no: 76% of nodes have no out-edges, 85% of first-hop targets are dead ends, out-degree is fixed at 6.
  It needs crawl expansion, and ranking currently ignores it (video_discoveries is only written, db.py:3381, and counted).

## 6. Real negative feedback (R)
- 4 videos hidden and marked not-interested: Carlos King (celebrity gossip), Walter Masterson (politics), LV Nation, Palace Confidential
  (royals). Corpus has 10, 1, 8, 11 other videos from those channels; 0 videos with cosine >0.5 to any of the four (titles are unique).
- Currently recommended: 0 of top 100 (both lanes) are from these channels or cos>0.5. In the scored pool LV Nation sits at ranks 618, 928,
  1821 and Carlos King at 785, 1997 of 2000. No leak today, but nothing prevents one: hide acts only on that video row
  (hide_count/not_interested in the WHERE); feedback_score, less_like_this, comment votes are video-local; no channel or topic penalty
  exists unless the user manually mutes (muted_channels/topics, both empty). Cosine > 0.5 is too sparse to generalise (0 neighbours).

## 7. Latency (dev, warm, one machine)
- list_recommended_videos(36, recommended): 795-885 ms. SQL execute 567 ms + fetch 2000 rows 45 ms (~70%); _apply_recommendation_scores
  215-264 ms (of which _subscription_interest_profile 87-90 ms, profile_vector 31, interest_exemplars 30, feed preferences 15, watch_time
  cold 19, takeout cold 12, scoring loop itself ~50); diversify 1.2 ms. Continue lane returns 1 row yet costs 591 ms. Empty user: same (877 ms).
- _home_feed_sections page 0: 2.30-2.40 s (R and N). It calls list_recommended_videos 3 times (recommended 54, continue 8, all 30 at
  offset 30) = 2,289 ms (96%), each with its own 2000-row SQL and profile rebuild. Other: subscription list 50 ms, shorts 18 ms.
  Redundantly recomputed per call: _subscription_interest_profile 3x (276 ms), profile_vector 3x (~100), interest_exemplars 3x (~95),
  prefs 4x (55): ~525 ms of pure repetition. Page 1: 870-885 ms (one call). Production's 3.7 s is not reproduced here (the extra
  ~1.3 s is outside these functions: HTML render, network, prod load); I cannot attribute it.
- Why is the SQL slow? It computes the score over all 82k rows with six LEFT JOINs and a global sort (nestloop off, 32MB work_mem), every request.

## 8. Home shelf composition (page 0, R; videos shown)
Order in code: shorts claimed first, recommended(54) -> lead 12, catch-up reserved, continue-watching(8), new-subscriptions(12), shorts(14),
"More recommendations" = lead overflow 18 + all-lane items at offset 30 (up to 30 more, deduped); catch-up inserted at slot 1+(seed%3).
R: recommended 12 (3 subscribed-channel, 10 channels, median age 2 d), continue 1, catch-up 8, new-subscriptions 12, shorts 14 (all
discovery), more 40 (24 subscribed, 1 live, 31 channels) = 87 videos; 20 are subscription-only shelves, 14 shorts. N: 12/0/8/12/14/39 = 85.
The "More" shelf mixes two rankers (recommended-lane overflow with subscription bias off, all-lane with +13 bias on).

## Ranked defects (largest first)
1. Pool = 2,000 newest rows: 93% of the corpus (every video >14 d old, all undated) is unreachable, 99.3% of subscribed-channel videos
   unevaluated, 98% of the profile-relevant videos (1,294 of 1,320 at cos>0.15) never scored. Max pool cosine 0.59 vs 1.0 in corpus. (s3)
2. No evaluation signal exists: evaluation.py returns "not enough history" for R and cannot score a watched-excluded feed; impressions and
   runs tables empty. Any weight change is currently unmeasurable. (s1)
3. Personalisation signal is nearly empty: 7 usable watches, 393 effective seconds, confidence 0.11; N gets 0 personal signal and a
   score range of 2.4-4.9 decided by recency bucket + subscription channel-name tokens (52% + 28%). (s1, s2)
4. Subscriptions (81 channels, 1,937 videos) never seed a taste model; their only use is channel-name token counts from the 500 newest
   videos (39 channels), i.e. biased toward the most prolific uploaders. (s1)
5. The "all" lane (used for "More recommendations" and deeper pages) is ordered by flat +13 subscription bonuses: 46% of score, ranks 1-20
   median 63 days old, 76% subscribed videos, 47 channels; two incompatible rankers share one shelf. (s2, s8)
6. Corpus noise rewarded by the pool: 15.4% live streams (22% of items <14 d), 15.9% trending, 14 of the top 100 are live; no content-type,
   quality or freshness-of-liveness filter. TF-IDF head terms are "live/news/now/every" and 22% of vectors have <=5 terms. (s4)
7. Negative feedback is video-local with zero generalisation; no channel/topic penalty is learned, and TF-IDF cosine finds 0 near-neighbours
   (cos>0.5) for any of the 4 negatives. Safe today only by luck of recency. (s6)
8. Latency: three full 2000-row SQL scans plus three profile rebuilds per home load (2.3 s of 2.4 s), including a 591 ms query for a
   1-row Continue shelf; profile pieces uncached (~525 ms of repetition). (s7)
9. Behavioural profile quality: stopword gaps ("here's", "i'd", "wanted") dominate the profile; watch_time_profile uses raw tags and a 60-day window that
   discards the heaviest watch; completed rows with 0 watched seconds count as 0.75 "completions". (s1)
10. video_discoveries (CF proxy) unused and thin: 76% of nodes unexpanded, fixed out-degree 6, 85% of 1-hop targets dead ends; yet endpoints are 22x
    more similar than random, so it is the best unexploited signal. ASCII-only tokenizer ignores 2.3% non-Latin titles. (s5, s4)
