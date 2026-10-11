
### R3 page (rv3/samples.jsonl)  n=76  load1 16.0-17.0
| stage | n | p50 ms | p95 ms |
|---|---|---|---|
| request setup (recall_projection+pending_visibility+freshness) | 76 | 9.4 | 28.1 |
| admission (filter_eligibility) | 0 | not run | not run |
| encode (vector.embed) | 76 | 32.7 | 116.2 |
| dense search (vector.search+vector.index) | 76 | 33.1 | 52.2 |
| bm25 | 76 | 29.2 | 55.8 |
| keyword | 76 | 39.4 | 90.6 |
| unit lanes (semantic_units) | 0 | not run | not run |
| graph | 76 | 26.3 | 135.4 |
| temporal | 10 | 43.6 | 124.4 |
| fusion+multipliers (fusion+lexical_guard) | 76 | 24.4 | 65.6 |
| hit construction (filter_hits) | 76 | 2.3 | 7.7 |
| serialize+release_gate | 76 | 6.4 | 18.0 |
| parent hints (probe, inside semantic.search, unspanned) | 76 | 84.7 | 132.3 |
| due state (probe, outside total_ms) | 76 | 20.7 | 58.1 |
| page hydration calls (probe page_resolve.n, count) | 76 | 92.0 | 150.0 |
| page cache reads (probe page_cache_get.n, count) | 76 | 126.0 | 187.0 |
| page hydration time (probe page_resolve.ms, spread over stages) | 76 | 44.6 | 124.6 |
| lexical catalogue connections (probe lex_connect.n, count) | 76 | 9.0 | 9.0 |
| catalogue readiness (probe, spread over lexical stages) | 76 | 27.2 | 46.0 |
| semantic.search residual (unspanned, after hints) | 76 | 5.0 | 9.6 |
| outside total_ms (wall - total_ms) | 76 | 23.3 | 60.3 |
| total_ms (server timings) | 76 | 337.9 | 577.6 |
| wall (ask_memory call) | 76 | 362.7 | 635.9 |

### R3 mixed (rv3/samples-mixed.jsonl)  n=76  load1 14.0-16.0
| stage | n | p50 ms | p95 ms |
|---|---|---|---|
| request setup (recall_projection+pending_visibility+freshness) | 76 | 7.6 | 11.6 |
| admission (filter_eligibility) | 0 | not run | not run |
| encode (vector.embed) | 76 | 0.0 | 0.0 |
| dense search (vector.search+vector.index) | 76 | 22.4 | 39.1 |
| bm25 | 76 | 12.6 | 26.0 |
| keyword | 76 | 11.0 | 25.3 |
| unit lanes (semantic_units) | 76 | 647.4 | 2142.9 |
| graph | 76 | 20.5 | 37.0 |
| temporal | 10 | 35.4 | 53.0 |
| fusion+multipliers (fusion+lexical_guard) | 76 | 21.3 | 43.4 |
| hit construction (filter_hits) | 76 | 2.1 | 6.1 |
| serialize+release_gate | 76 | 5.7 | 8.4 |
| parent hints (probe, inside semantic.search, unspanned) | 76 | 72.1 | 92.9 |
| due state (probe, outside total_ms) | 76 | 18.1 | 26.8 |
| page hydration calls (probe page_resolve.n, count) | 76 | 95.0 | 150.0 |
| page cache reads (probe page_cache_get.n, count) | 76 | 442.0 | 497.0 |
| page hydration time (probe page_resolve.ms, spread over stages) | 76 | 31.2 | 60.0 |
| lexical catalogue connections (probe lex_connect.n, count) | 76 | 10.0 | 10.0 |
| catalogue readiness (probe, spread over lexical stages) | 76 | 17.9 | 33.0 |
| semantic.search residual (unspanned, after hints) | 76 | 6.0 | 11.9 |
| outside total_ms (wall - total_ms) | 76 | 20.2 | 29.9 |
| total_ms (server timings) | 76 | 853.6 | 2419.3 |
| wall (ask_memory call) | 76 | 871.3 | 2460.0 |

### R1 page (rv1/samples.jsonl)  n=38  load1 16.8-19.5
| stage | n | p50 ms | p95 ms |
|---|---|---|---|
| request setup (recall_projection+pending_visibility+freshness) | 38 | 17.5 | 81.3 |
| admission (filter_eligibility) | 0 | not run | not run |
| encode (vector.embed) | 38 | 66.7 | 151.5 |
| dense search (vector.search+vector.index) | 38 | 56.1 | 100.7 |
| bm25 | 38 | 56.6 | 77.5 |
| keyword | 38 | 90.2 | 175.8 |
| unit lanes (semantic_units) | 0 | not run | not run |
| graph | 38 | 52.6 | 120.5 |
| temporal | 5 | 96.9 | 151.7 |
| fusion+multipliers (fusion+lexical_guard) | 38 | 53.6 | 138.5 |
| hit construction (filter_hits) | 38 | 4.9 | 35.2 |
| serialize+release_gate | 38 | 10.8 | 51.4 |
| semantic.search residual (unspanned, after hints) | 38 | 168.9 | 313.4 |
| outside total_ms (wall - total_ms) | 38 | 23.6 | 65.9 |
| total_ms (server timings) | 38 | 651.0 | 846.4 |
| wall (ask_memory call) | 38 | 699.5 | 865.7 |

### R2 page (rv2/samples.jsonl)  n=38  load1 16.7-17.5
| stage | n | p50 ms | p95 ms |
|---|---|---|---|
| request setup (recall_projection+pending_visibility+freshness) | 38 | 16.4 | 107.5 |
| admission (filter_eligibility) | 0 | not run | not run |
| encode (vector.embed) | 38 | 40.6 | 165.1 |
| dense search (vector.search+vector.index) | 38 | 40.8 | 142.7 |
| bm25 | 38 | 43.5 | 92.2 |
| keyword | 38 | 97.1 | 353.0 |
| unit lanes (semantic_units) | 0 | not run | not run |
| graph | 38 | 38.1 | 319.9 |
| temporal | 5 | 56.0 | 175.6 |
| fusion+multipliers (fusion+lexical_guard) | 38 | 35.8 | 289.5 |
| hit construction (filter_hits) | 38 | 3.6 | 22.0 |
| serialize+release_gate | 38 | 9.1 | 64.3 |
| semantic.search residual (unspanned, after hints) | 38 | 130.0 | 473.7 |
| outside total_ms (wall - total_ms) | 38 | 27.3 | 549.7 |
| total_ms (server timings) | 38 | 549.2 | 1630.5 |
| wall (ask_memory call) | 38 | 619.1 | 1798.6 |

### M1 page (mf2/samples.jsonl)  n=38  load1 24.9-25.6
| stage | n | p50 ms | p95 ms |
|---|---|---|---|
| request setup (recall_projection+pending_visibility+freshness) | 38 | 7.9 | 15.8 |
| admission (filter_eligibility) | 0 | not run | not run |
| encode (vector.embed) | 0 | not run | not run |
| dense search (vector.search+vector.index) | 0 | not run | not run |
| bm25 | 38 | 35.0 | 83.5 |
| keyword | 38 | 176.9 | 341.5 |
| unit lanes (semantic_units) | 0 | not run | not run |
| graph | 38 | 34.2 | 171.4 |
| temporal | 5 | 76.7 | 113.4 |
| fusion+multipliers (fusion+lexical_guard) | 38 | 64.9 | 103.8 |
| hit construction (filter_hits) | 38 | 2.2 | 6.9 |
| serialize+release_gate | 38 | 4.1 | 14.9 |
| semantic.search residual (unspanned, after hints) | 38 | 55.2 | 131.8 |
| outside total_ms (wall - total_ms) | 38 | 21.0 | 39.2 |
| total_ms (server timings) | 38 | 398.1 | 731.0 |
| wall (ask_memory call) | 38 | 428.7 | 753.1 |

### A1 page (mf1-raw-admission/samples.jsonl)  n=38  load1 18.8-28.3
| stage | n | p50 ms | p95 ms |
|---|---|---|---|
| request setup (recall_projection+pending_visibility+freshness) | 38 | 10.2 | 98.8 |
| admission (filter_eligibility) | 38 | 214.2 | 439.7 |
| encode (vector.embed) | 0 | not run | not run |
| dense search (vector.search+vector.index) | 0 | not run | not run |
| bm25 | 38 | 701.2 | 946.8 |
| keyword | 38 | 185.2 | 518.4 |
| unit lanes (semantic_units) | 0 | not run | not run |
| graph | 38 | 18.4 | 50.7 |
| temporal | 5 | 2828.2 | 4066.8 |
| fusion+multipliers (fusion+lexical_guard) | 38 | 2331.0 | 5008.6 |
| hit construction (filter_hits) | 38 | 2.7 | 14.6 |
| serialize+release_gate | 38 | 10.0 | 50.2 |
| semantic.search residual (unspanned, after hints) | 38 | 2807.3 | 4067.5 |
| outside total_ms (wall - total_ms) | 38 | 25.1 | 37.9 |
| total_ms (server timings) | 38 | 6809.1 | 9969.6 |
| wall (ask_memory call) | 38 | 6836.6 | 9992.4 |
