# Retrieval and content representation: literature review for OpenFeed
Evidence grade per item: [A] = abstract/page read by me this session; [S] = secondary summary only (search snippet), full text not read;
[M] = from memory, NOT verified this session, treat as unverified. No numbers below are measured on OpenFeed data. No user data left the machine.
Setting: 1-2 users, <20 watches, 81 subs, 80k candidates, no other users, no labels, one CPU server, no data egress.
## 1. Text representation
1. BM25 vs TF-IDF (Robertson). Core finding in IR: saturating tf and length normalisation beat raw tf-idf for short-query retrieval. [M]  Transfer: weak. That evidence is query-to-document. Our task is document-to-document (profile centroid vs candidate), where cosine on
   L2-normalised sublinear tf-idf is the standard and BM25's query-side saturation has no clear analogue. For titles (5-15 tokens) length
   normalisation hardly matters. Expect small differences; do not spend effort here before evaluation exists.
2. BM25F (Robertson, Zaragoza, Taylor, CIKM 2004) https://ir.webis.de/anthology/2004.cikm_conference-2004.6 [S]. Measured on Reuters Vol1 and
   TREC .gov; key point: weight per-field term frequencies BEFORE the saturation function, because summing per-field scores breaks saturation.  Transfer: the principle (title x3, channel x2, description x1 inside one tf, not three summed cosines) transfers and is free. Their
   gains were on query retrieval; no evidence for recommendation. Check that description noise (links, sponsor boilerplate, hashtags) is
   stripped first; that is probably worth more than the weighting.
3. BEIR (Thakur et al. 2021) https://arxiv.org/abs/2104.08663 [A]: across 18 datasets zero-shot, BM25 is a robust baseline; dense retrievers
   often underperform out of domain; re-rankers best but costly. Transfer: direction supports "do not assume embeddings beat lexical".
   Caveat: query-passage retrieval, not profile-based recommendation.
4. News recommendation with PLMs (Wu et al. 2021) https://arxiv.org/abs/2104.07413 [A abstract only]. PLM news encoders improved over traditional
   text models on MIND and a multilingual dataset, and gave online click/pageview gains at Microsoft News. Numbers not read. Transfer: weak.
   Those encoders are fine-tuned end-to-end on millions of clicks. Off-the-shelf frozen embeddings with no fine-tuning are a different
   regime, and I found no source I read that measures frozen embeddings vs TF-IDF for content recommendation. Evidence gap, state as such.
5. Dacrema et al. 2019 https://arxiv.org/abs/1907.06902 [A]: of 18 neural top-n methods only 7 reproducible; 6 of those were beaten by tuned
   kNN/graph heuristics, the 7th by a tuned linear model. Transfer: strongest argument for keeping OpenFeed non-neural and for building
   a real evaluation before adding model complexity. Their datasets (MovieLens, Pinterest etc.) have thousands of users, so it supports
   "simple is competitive", not any specific number for us.
6. Sentence embeddings. SBERT https://arxiv.org/abs/1908.10084 [A]: cosine-comparable embeddings; 10k-sentence pair search 65 h with BERT to
   about 5 s. LaBSE https://arxiv.org/abs/2007.01852 [A]: 109+ languages, 83.7% Tatoeba bitext accuracy over 112 languages; it is a
   translation-pair model (aligned meaning), ~470M params [M]. Multilingual distillation https://arxiv.org/abs/2004.09813 [A]: the
   paraphrase-multilingual-* family aligns translations to English teacher space for 50+ languages. paraphrase-multilingual-MiniLM-L12-v2
   is ~118M params, ~0.44 GB fp32 [S, HF MTEB listing]. A conflicting tutorial claimed 385M; I discard it.
   Cost for 80k titles: I found NO sourced CPU throughput figure and did not benchmark. My unverified estimate: 12-layer MiniLM on short
   inputs (~24 tokens) on a few modern cores is tens to low hundreds of texts/s, so 80k is roughly 10-60 minutes once, ONNX int8 faster.
   Treat as a hypothesis to measure, not a fact. Storage: 80k x 384 float32 = 123 MB (float16 61 MB); brute-force matmul is ~30M mult-adds,
   under 50 ms. No ANN needed at this size.
   Does it transfer: the dependency cost is large (onnxruntime + tokenizers + ~120-450 MB model download; model download happens once and
   is not user data, so privacy rule holds if the file is vendored). The benefit is mainly cross-language semantics, which only matters
   if the household actually consumes multiple languages. Quantify the language mix of the corpus first.
7. Multilingual lexical. Character n-grams (2-3 for CJK, Japanese/Chinese without a segmenter) are standard practice and dependency-free [M];
   my search did not return a primary paper to cite. For scripts with spaces (Cyrillic, Korean hangul words, Arabic), the fix is only
   a Unicode-aware regex (`\w+` with re.UNICODE, casefold, NFKC) instead of the ASCII TERM_RE. Hashing n-grams into a fixed dimension
   (HashingVectorizer style) avoids vocabulary growth. Evidence is practitioner knowledge, not measured on recommendation.
## 2. Graph-based retrieval
1. Pixie (Eksombatchai et al., WWW 2018) https://arxiv.org/abs/1711.07601 [A]. Biased random walks with restart from a weighted query set
   over a 3B-node/17B-edge pin-board graph; up to +50% engagement vs prior Hadoop system and +58% from graph pruning (removing
   high-degree/unrelated edges); 1200 req/s per server, 60 ms. The paper's own lesson: pruning noisy edges and biasing the walk toward the
   user's context beat using the raw graph. Transfer: algorithm transfers cleanly (our graph has 117k edges; a walk is milliseconds in
   pure Python/numpy/scipy sparse). Their engagement gains rely on a huge, engagement-built graph; ours is a related-videos crawl.
2. RP3beta / P3alpha (Christoffel, Paudel et al., RecSys 2015 "Blockbusters and Wallflowers", TiiS 2017 doi 10.1145/2955101) [S].
   3-hop walk user-item-user-item with item-degree penalty (beta; irspack default 0.6) gives accurate recommendations with a larger long-tail
   share; ~100 ms updates. Dacrema 2019 reports these graph methods among the strongest simple baselines [A, abstract]. A 2025 comparison
   https://arxiv.org/abs/2504.08457 [A] groups EASE-R and RP3beta as best accuracy/scalability trade-off. Did not read the effect sizes.  Transfer: the popularity-discount idea transfers directly to our item-item graph: score(j|i)= w_ij / deg(j)^beta. In-degree of a video in a
   YouTube related crawl is a popularity proxy (YouTube's related lists funnel to hubs), so degree-discounting is arguably more needed
   here than in user-item data. The user-user hop does NOT exist for us (no population), so use item-item walks, not RP3's full form.
3. Using a third-party recommender's output as signal. I found no paper that evaluates this for a personal recommender. What exists: audits
   show related-video crawls depend on sampling method (Karkulahti and Kangasharju, TMA 2015, "YouTube Revisited",
   https://dl.ifip.org/hal-01411177v1 [S]) and that crawled recommendation graphs embed the platform's own personalised/biased edges
   (Antoniadis et al. https://arxiv.org/abs/1704.08991 [S]); a 2026 channel census paper notes recursive recommendation crawls introduce
   selection bias [S, https://arxiv.org/pdf/2605.06999, not read]. Implications for us (my inference, not a cited result):
   (a) seeds shape the reachable set, so nodes far from seeds have few edges; edge count is partly a crawl-coverage artifact, not
   popularity; (b) the graph encodes YouTube's engagement objective, so it will pull toward high-watch-time content; (c) a node with no
   edges is "unknown", not "unrelated". Treat graph score as a retriever of candidates, calibrate with in/out-degree normalisation, and
   never penalise nodes for missing edges. The only valid way to know its value is to test whether graph neighbours of watched videos
   predict later watches/likes (an offline hit test on the user's own history, with the 20-watch caveat).
## 3. Candidate generation, indexes, precompute
1. Covington et al., RecSys 2016 (YouTube DNN) https://research.google/pubs/deep-neural-networks-for-youtube-recommendations/ [M, not fetched].
   Structure: cheap candidate generation over the whole corpus, then a richer ranker over hundreds. Ideas that survive without training:
   the two-stage funnel itself, example-age feature (freshness), and not letting one heavy user/channel dominate. Training, softmax over millions,
   watch-time-weighted logistic regression do not transfer (no labels).
2. Scale: 80k x 384 dense or 80k sparse tf-idf is trivial for brute force. A sparse matrix-vector product over 80k rows with ~15 nnz each is ~1M ops,
   ~ms. Therefore ANN (FAISS/pgvector HNSW) and GIN indexes are unnecessary; the SQL pool of 2000 by recency is a design limitation
   with no performance justification. Score all 80k in numpy per request, or hold the tf-idf CSR matrix in process memory
   (about 80k x 15 x 12 B = ~15 MB). pgvector would add an extension for no gain at this size [reasoning, not citation].
3. Precompute vs per-request: with 1-2 users, per-request full-corpus scoring (few ms to tens of ms) beats caches and avoids staleness
   after feedback. Precompute only expensive static parts (item vectors, graph degree, channel stats, quality prior), refreshed by the
   precomputing per user [A].
## 4. Fusion
1. RRF (Cormack, Clarke, Buettcher, SIGIR 2009) https://plg.uwaterloo.ca/~gvcormac/cormacksigir09-rrf.pdf [S]: sum of 1/(k+rank), k=60
   chosen in a pilot (MAP flat near 60) beating Condorcet, CombMNZ and the best single system in their TREC fusion tests. Scale-free,
   needs no labels and no tuning.
2. Bruch, Gai, Ingber, "An analysis of fusion functions for hybrid retrieval" https://arxiv.org/abs/2210.11934 [A]: RRF is sensitive to its
   parameter; convex combination of normalised scores beats RRF in- and out-of-domain; CC is sample-efficient, needing few labelled
   examples to tune its single weight; learning is largely agnostic to normalisation choice. Contradicts the "RRF is untunable-safe" folk
   claim. Transfer: lexical vs dense hybrid on queries with hundreds of labels; ours has ~tens. A single-weight CC with a prior weight
   and rank/z-score normalisation is the viable design; RRF is the label-free fallback. Contradiction between 1 and 2 is real; unresolved here.
3. Why raw additive boosts break: each signal has a different distribution (cosine in [0,0.3], log views in [0,15], recency in [0,1]); the
   weight then encodes scale, not importance, and the heaviest-tailed signal dominates. CombSUM on unnormalised scores is the same failure
   (Fox and Shaw 1994, Lee 1997 on normalisation and CombMNZ [M]). Fix: per-signal normalisation to percentile rank or robust z-score over
   the candidate pool before weighting. Reasoning plus Bruch result above; not shown on recsys data.
4. Few labels: LambdaMART (Burges) needs hundreds-thousands of queries [M]; infeasible. Viable: (a) fixed priors; (b) tune 1-3 weights by
   coordinate ascent/grid on a proxy metric from implicit signals (hit-rate of held-out watches in top-N), with strong shrinkage to the prior;
   (c) Bayesian updates of a weight per signal from like/skip events (Beta-Bernoulli on "was the top item from signal X engaged?").
   With <20 watches the proxy has far too much variance to select among more than ~2 candidates; unclear/no direct literature read.
   Prefer interleaving/bandit-style online evaluation over offline selection. No source read for (c); it is design reasoning.
## 5. Channel-level modelling
No paper read for this section; this is standard-method reasoning. Use empirical-Bayes shrinkage of per-channel engagement toward the global mean
(pseudo-count m about 5) so one skip cannot zero a channel; treat a subscription as a positive prior event (81 channels carry more signal than 20
watches); negatives update channel and topic posteriors with decayed weight. Channel similarity without other users: degree-normalised co-occurrence
in the related graph (inherits crawl bias, 2.3) plus tf-idf centroid cosine.
## 6. Quality and popularity priors
1. Cremonesi, Koren, Turrin RecSys 2010 doi 10.1145/1864708.1864721 [S]: non-personalised top-popular is competitive on full data with many
   algorithms, but drops sharply once the very top head items are removed ("long tail" test). Lesson: accuracy metrics reward popularity
   unless the test set is built to avoid it. Transfer: our offline test, if built from watches, will reward popular candidates; report a
   popularity-only baseline and a long-tail variant alongside any score.
2. Ji, Sun, Zhang, Li SIGIR 2020 https://arxiv.org/abs/2005.13829 [A]: time-aware popularity (items popular at interaction time) improves
   MovieLens MostPop by 70%+; lighter users follow the crowd more, heavy users follow personal taste. Transfer: recency-decayed popularity
   (view velocity: views/age, which we can compute from 72k view counts and publish dates) is the right form of the prior; with a
   sparse-history user, popularity prior should have more weight, shrinking as evidence accrues. Single movie dataset, ratings not videos.
3. Abdollahpouri et al. https://arxiv.org/abs/1907.13286 [A]: algorithms concentrate on popular items even for niche-leaning users
   (MovieLens); popularity bias hurts niche users most. Transfer: argues for degree/popularity discounting in the graph walk and for a
   cap on popularity weight; it measures harm to niche-leaning users, who are plausible for a subscription-heavy self-hosted user.
4. Like ratio / Wilson lower bound: YouTube dislike counts are not exposed publicly since 2021 [M], so ratio data probably isn't available;
   verify what Invidious returns. Wilson bound requires counts of both outcomes; with only likes+views it would be a smoothed like-rate
   (likes/views) with Beta prior, not a Wilson. Not read: Wilson 1927, Miller's blog. Do not claim effect.
5. Net: popularity is a useful cold-start prior and a bad dominant term in a 80k set where 79k come from discovery (already popularity-
   filtered by YouTube). Weight it down as history grows, and normalise views within channel (views / channel median) to measure
   surprise-popularity rather than channel size.
## Not found / weak
No source read compares frozen embeddings with TF-IDF for video recommendation; no MiniLM CPU throughput source; no paper on using a crawled third-party related-graph for a personal recommender; no small-label LTR paper (4.4 is design reasoning); Pixie/RP3 full texts unread.
## Falsifiable hypotheses
H1. Change: replace additive boosts with per-signal percentile normalisation, then weighted sum (CC). Effect: higher hit@50 of held-out future watches/likes than current additive scorer, same candidate pool. Falsified if: paired bootstrap CI of delta hit@50 includes 0 or is negative over leave-one-out splits on the real user, or Spearman between old and new ranks >0.95. Cost: ~40 lines numpy, no deps, no memory.
H2. Change: widen scoring from the 2000-row pool to the full 80k via in-memory tf-idf CSR matrix. Effect: more distinct channels and higher recall of held-out liked videos (some are outside the 2000 pool). Falsified if recall@100 of held-out positives rises <2 points or p95 latency >150 ms. Cost: ~15 MB resident, no deps beyond scipy (check already installed); rebuild on worker cycle.
H3. Change: seed profile with the 81 subscription channels' video centroids (weight < watch weight). Effect: non-empty profile for the real user; hit@50 above the recency baseline. Falsified if it does not beat recency order on the same holdout. Cost: negligible.
H4. Change: Unicode-aware tokenizer plus 2-3 char n-grams for CJK. Effect: coverage of videos with a nonzero vector rises from the current share to near 100% of non-empty titles; no drop in ASCII-only hit@50. Falsified if coverage is unchanged, or ASCII hit@50 drops >1 point. Cost: regex change, vocabulary grows (cap with min_df=3 or hashing to 2^18).
H5. Change (incl. degree discount beta=0.6 vs 0): item-item random walk with restart on video_discoveries from the user's positive videos with beta degree discount (beta in {0,0.3,0.6}). Effect: graph score predicts later positives better than random and than tf-idf alone when fused. Falsified if hit@50 of graph-only <=  popularity-only baseline, or fusion adds <1 point over tf-idf alone. Cost: scipy sparse 117k edges (<5 MB), no new deps.
H7. Change: channel affinity as Beta-smoothed estimate (pseudo-count m=5, prior = subscription status). Effect: a single hide/skip no longer kills a channel; fewer repeats of channels with 2+ negatives. Falsified if channels with >=2 negatives still appear in top-50 at the same rate. Cost: small table keyed by channel, no deps.
H8. Change: add multilingual MiniLM (ONNX int8) embeddings as a second retriever, fused with tf-idf. Effect: improves hit@50 on non-English titles specifically. Falsified if non-English hit@50 gain <2 points or embedding 80k takes >1 hour on this CPU. Cost: onnxruntime + tokenizers, ~120-450 MB model, 123 MB vectors (61 MB fp16). Only worth testing after H1-H4 and if corpus language mix is >15% non-English.
H9. Change: time-aware popularity prior (views/age, normalised within channel) weighted by 1/(1+n_positive_events). Effect: better cold-start ordering than recency. Falsified if hit@50 is not above recency-only baseline with <10 events, or it dominates the top 20 with >50% items above the corpus 90th view percentile. Cost: one column computed at ingest.
H10. Change: evaluation scaffolding itself: leave-one-out on positives, candidates NOT excluding watched-in-train only for the held-out item, report popularity and recency baselines. Effect: nonzero, stable baselines. Falsified if baseline hit@50 for recency and popularity are both 0 on the real user's history (then evaluation can't discriminate and online interleaving is the only route). Cost: none.