# Feel audit: plan and targets, written 2026-11-08 before any variant was run

**Why this audit.** The sealed test measured relevance only. Its pools were age-matched by
design, so freshness was invisible to it. The real profile's Recommended shelf looks like
this:

- The median age of the top 60 is about 57 days, and nothing is newer than 18 days.
- 28-view and 878-view videos sit next to million-view ones.
- Every reason reads "Linked from 1 video you watched or liked".

The library is not short of fresh videos: it holds 6,388 under 14 days old. A graph walk
favours old videos, because they have had longer to collect related-list links. The
diagnosis: v2 is relevant but feels stale, and its reasons are vague.

## Changes, each switchable for measurement

1. **Freshness (F)**
   - Factor: `F = max(FLOOR, 2^(-age/H))`.
   - Applied as `g × F^β`, so a video needs 1/F^β times the relevance to outrank a fresh one.
   - Applies to graph mode only.
2. **Quality (Q).** `Q = 0.5 + 0.5 × velocity percentile`, applied as a multiplier.
3. **Dead-video floor.** Exclude a video when its views are known, below 100, and it is more
   than 7 days old.
4. **Rotation**
   - Multiply by 0.75^k, where k is the number of distinct earlier days (cap 3) on which the
     video was shown in the last 7 days.
   - Computed from `feed_impressions` older than 30 minutes, so one visit's pages stay
     consistent.
   - This is product policy. It is not measurable offline.
5. **Reasons**
   - Name the strongest seed: "Because you watched <title>" or "Because you follow <channel>".
   - Mention "New" for videos under 3 days old.

## Measures

**Relevance**, on the live dev copy of the library:
- leave-one-subscribed-channel-out (LOCO), nDCG@20 and recall@100
- an age-matched LOCO variant, in which negatives come only from the held-out channel's
  publication weeks. This removes any reward for freshness itself.

**Feel**, on the real profile's top 24 recommended:
- median age, the share 14 days old or less, the share with fewer than 1k views
- distinct channels, the share over 3 hours

**Rotation**
- overlap of the top 24 between two visits a day apart, simulated through the impression log

**Preference**
- 20 blind pairwise judges (Haiku)
- Each gets the profile (the subscribed channel names and the titles watched) and two
  top-24 lists in random order, A or B, and picks the feed it would rather open.

## Targets and the selection rule

The grid is H ∈ {14, 21, 30} days, β ∈ {0.5, 1}, FLOOR 0.25, γ = 1.

1. Of the configs that meet the feel targets, choose the one with the highest age-matched
   LOCO nDCG@20.
2. It ships only if:
   - age-matched LOCO nDCG@20 ≥ 0.85 × current graph mode (with low power, this is a
     descriptive guard)
   - the median age of the top 24 is 21 days or less, and at least 40% are 14 days old or less
   - videos with fewer than 1k views are 5% or less
   - at least 22 distinct channels appear in the top 24
   - the judges prefer it, with the Wilson 95% lower bound above 50%
   - warm p95 ranking time stays at 150 ms or less
3. If no config meets every target, report that and ship nothing that misses relevance.

## Amendment 1 (2026-11-08): written after the grid, before any judge ran

**The freshness multiplier failed.**
- The real profile's top-24 median age only moved from 58 to between 49 and 53 days.
- Every setting cut LOCO relevance: plain LOCO by 10% to 67%, age-matched by 14% to 48%. All
  but H14_b0.5 broke the 0.85 guard.

The cause is seed concentration. The few events the user watched each carry about 100 times the
seed mass of a subscription video, so their September neighbours fill every top slot. Track B
blended z-scored terms, which equalises scale. The port mixed raw seed mass, which does not, so
"personal share n/(n+K)" had become roughly 100% of the top slots.

**Replacement: slot mixing of three candidate sources.** It is what the YouTube papers describe
(several candidate generators, then a blended page) and is closer to track B's scale-free blend.

| Source | What it holds | Order |
|---|---|---|
| Channels | Walk from subscriptions only | walk × Q |
| Watched | Walk from graded events only | walk × Q |
| Fresh | Videos up to 14 days old reached by either walk | (sum of max-normalised walks) × Q |

- Shares: Watched gets n/(n+K), Fresh gets 0.2 of the rest, and Channels the remainder.
- Q stays (LOCO +7% plain, −6% age-matched). The freshness multiplier is removed (β = 0).

**Measured before the change, LOCO, subscription seeds:**

| Variant | Plain LOCO | Age-matched LOCO |
|---|---|---|
| Graph | 0.092 | 0.216 |
| Graph × Q | 0.099 | 0.203 |
| Plus 20% fresh slots | 0.084 | 0.188 |
| Plus 30% fresh slots | 0.074 | 0.179 |

20% passes the 0.85 guard and 30% does not, so 20% it is.

**The choice of K is left to the judges, because LOCO cannot see events.**
- Candidates: (a) the current graph, (b) the mix with K = 5 (track B's T2 choice), (c) the mix
  with K = 20 (the earlier conservative choice).
- Pairwise blind judges: b vs a, c vs a, b vs c, 20 per pair.
- Ship the judge winner among the variants that pass the relevance guard and the views,
  channels and latency targets.
- The age targets are reported. They cannot be met at a 20% fresh share, and they are not
  relaxed after the fact.

## Results of Amendment 1 (judges, 2026-11-08)

Blind pairwise judges (Haiku), 20 per pair, sides randomised. The judges picked side A 8 to 11
times out of 20, so they showed no side bias.

| Pair | Result | Wilson 95% interval |
|---|---|---|
| Mix K=5 vs current | the mix preferred 20/20 | [0.84, 1.00] |
| Mix K=20 vs current | the mix preferred 20/20 | [0.84, 1.00] |
| Mix K=20 vs mix K=5 | K=20 preferred 20/20 | [0.84, 1.00] |

The judges' reasons were consistent. The mix brings in the person's actual subscriptions and
interests (cars, cooking, faith, privacy, business). Watch-heavy feeds drift into sports and
celebrity drama from a few old watches.

**Shipped: K = 20.** On the real profile it met the views, channels and latency targets and
missed the age targets (median 83 days, 17% under 15 days), as stated in advance. LOCO was
0.084 plain and 0.188 age-matched (current: 0.092 and 0.216), inside the 0.85 guard.

## Amendment 2 (2026-11-08): after an independent code audit, before the confirmatory judges

1. **Rotation decay calibrated to its stated target.** The target is that at most 60% of the top
   24 repeats a day later.
   - 0.75 repeated 75% (18/24) of the top 24 a day later. It missed.
   - 0.7 repeats 58% (14/24) and 0.65 repeats 46%.
   - The ship is 0.7: the most continuity that still meets the target.
   - Rotation counts only days before today, as written above. The first code also counted
     today, which the audit caught. The user cache key is now the date, so one day's pages agree
     with each other.
2. **Audit fixes, not tuned on any metric:**
   - progress stored on a 0-100 scale is normalised
   - "Less like this" counts as a negative
   - Undo of a hide is respected
   - Home reads each lane contiguously; ranks 30-59, 90-149 and so on were never shown before
   - the classic fallback keeps the lane rules
   - the related lists are cached to disk, so a restart takes 1.5 s instead of 42 s
   - one cursor per call
3. **Watched source:**
   - its share is capped at 0.5
   - events decay with a 60-day half-life, floored at 0.25. This is a product choice; T2 showed
     no effect of decay
   - picks rotate across seed videos, because one watched video drove 7 of the 13 watched slots
4. **Fresh source changed to "new from related creators".**
   - Creator affinity is the sum of a channel's top-3 walk scores.
   - Candidates are the creator's uploads of 30 days or less, ordered by affinity × Q × 2^(−age/30),
     round-robin across creators.
   - LOCO on subscription seeds, measured before adoption:

     | Variant | Plain LOCO | Age-matched LOCO |
     |---|---|---|
     | Current fresh source | 0.084 | 0.188 |
     | Creator source, 0.3 share, 30 days | 0.148 | 0.292 |
     | Creator source, 0.4 share, 30 days | 0.163 | 0.306 |

   - Caveat: LOCO positives are one whole channel, so any channel-level aggregation is favoured
     by construction. The true gain is smaller than shown, but the change is not a regression on
     either measure.
   - Shipped share: 0.3. It is the larger gain that keeps channels as the majority source; 0.4
     was not chosen, to avoid one-creator floods.
5. **Confirmatory judges:** 20 per pair, final vs current, and final vs the shipped K = 20 mix.
   Reported as they come.

## Results of Amendment 2 (2026-11-08)

**Confirmatory blind judges**, 20 per pair, sides randomised:

| Pair | Final preferred | Wilson 95% | Sign p | Judges picking side A |
|---|---|---|---|---|
| Final vs current (pre-audit) | 19/20 | [0.76, 0.99] | < 0.001 | 10/20 |
| Final vs the shipped K=20 mix | 14/20 | [0.48, 0.85] | 0.12 | 6/20 |

Final vs current is decisive. Final vs the K=20 mix favours the final version but is not
significant on 20 judges. The judges credited the final feed with fresh tech and privacy
uploads from related creators, and with fewer off-profile picks.

**The real profile, top 24, final vs current:**

| Measure | Final | Current |
|---|---|---|
| Distinct channels | 24 | 23 |
| Under 1k views | 0% | 4% |
| Median views | 342k | 138k |
| Median age | 85 days | 58 days |
| Published in the last 30 days | 6 | 0 |
| Cold rank p95 | 90 ms | |

The source mix was 14 channels, 6 new-from-related-creators and 4 watched.

**LOCO on the shipped code:**

| | Final | Current |
|---|---|---|
| Plain | 0.108 | 0.092 |
| Age-matched | 0.217 | 0.216 |

The creator round-robin removes most of the channel-level advantage that the experiment's
0.148 contained.

**Not met:** the pre-registered age targets (median 21 days or less, at least 40% within 14
days). The channels source reaches popular evergreen videos. Making it fresher cost relevance
on every measure tried (Amendment 1). Fresh content on Home comes from the subscription
shelves (median age 1 day) and from the new-from-related-creators slots.
