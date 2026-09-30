## ADDED Requirements

### Requirement: The Ledger Write Site Feeds A Rolling Latency Watch

The site that records each MCP call's ledger row SHALL also feed an in-process
latency watch with the call's tool name, calling client, whether the call was a
deep recall, its `total_ms`, its timestamp and its dominant stage spans. The
watch SHALL be bounded in entries, SHALL hold no query text, path, excerpt or
argument value, and SHALL follow the ledger's own rule that a failure in it
never breaks or slows the call. The watch SHALL report, per tool, client and
deep flag over a trailing window, the sample count and the p50 and p90 of
`total_ms`, SHALL compare the p90 against a ceiling held as a provisional
constant in one module, SHALL require a minimum sample count before reporting a
breach, SHALL exclude samples recorded within a startup grace period from the
verdict, and SHALL name the stage spans that dominate the calls over the
ceiling. When a (tool, client, deep) first breaches its ceiling the watch SHALL
log one structured `latency_ceiling_exceeded` event with those content-free
fields, at most once per report interval per key.

#### Scenario: A regression is named by its dominant stage

- **WHEN** at least the minimum number of plain recalls from one client in the window have a p90 over the ceiling, and most of the time in the slow calls sits in one stage
- **THEN** the watch reports a breach for that tool and client with the sample count, p50, p90 and ceiling
- **AND** that stage is first among the dominant spans, with its total milliseconds and call count

#### Scenario: The cold window after a promotion is not a regression

- **WHEN** the only calls over the ceiling were recorded within the startup grace period
- **THEN** the watch reports no breach for that tool and client

#### Scenario: Too few calls is not a verdict

- **WHEN** fewer than the minimum sample count exist for a tool and client in the window
- **THEN** the watch reports the figures it has and no breach

#### Scenario: A failing watch costs the call nothing

- **WHEN** the watch raises while observing a call
- **THEN** the ledger row is still written unchanged
- **AND** the response the client receives is unchanged
