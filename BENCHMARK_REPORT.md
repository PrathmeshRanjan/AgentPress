# AgentPress Benchmark Report

Generated from 1 completed paired runs. Image generation was disabled to isolate text orchestration.

> Benchmark status: partial (1/5 default seed topics). Provider daily quotas prevented completion of the remaining paired runs; use `python benchmark.py --resume` after quota reset.

## Aggregate Results

| System | Average Latency | Total Tokens | Total Estimated Cost | Average Revision Loops |
|---|---:|---:|---:|---:|
| Vanilla Baseline | 32,358 ms | 5,922 | $0.0146 | 0.00 |
| Multi-Agent Pipeline | 143,138 ms | 74,677 | $0.0937 | 0.00 |

## Interpretation

- Multi-agent token overhead: +1161.0%.
- Multi-agent latency overhead: +342.4%.
- Circuit-breaker outcomes: 0/1 runs completed with unresolved errors flagged.
- Quality is intentionally left to the paired human-review output; factual-marker density is a descriptive proxy, not a quality score.

## Resume-Ready Bullets

- Architected a non-linear multi-agent content pipeline with Editor and Fact-Checker critique loops, durable feedback state, and a three-iteration circuit breaker that prevents unbounded token spend.
- Benchmarked the pipeline against a one-shot baseline across 1 seed topic, measuring 74,677 average tokens and $0.0937 estimated model cost per task.
- Implemented per-agent production telemetry for latency, prompt/completion tokens, model-aware cost, and end-to-end runs; quantified +1161.0% token and +342.4% latency overhead versus the baseline.

## Per-Topic Results

| Topic | Baseline Tokens | Pipeline Tokens | Pipeline Revisions | Pipeline Cost | Status |
|---|---:|---:|---:|---:|---|
| Why idempotency is the hidden foundation of reliable distributed systems | 5,922 | 74,677 | 0 | $0.0937 | completed |
