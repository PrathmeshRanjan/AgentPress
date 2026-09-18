# AgentPress Benchmark Report

Generated from 3 completed paired runs. Image generation was disabled to isolate text orchestration.

## Methodology

- Topics: 3 paired prompts, with both systems using the same model fallback chain.
- Output target: 2 pipeline sections at approximately 300 words each; the baseline received a matched total-word target.
- Maximum benchmark revisions: 1. The production circuit breaker remains capped at three revisions.
- Web research and image generation were disabled for both systems to isolate text orchestration cost.
- Quality review uses blinded A/B outputs and a 25-point human rubric covering accuracy, coherence, depth, clarity, and usefulness.

## Aggregate Results

| System | Average Latency | Total Tokens | Total Estimated Cost | Average Revision Loops |
|---|---:|---:|---:|---:|
| Vanilla Baseline | 13,417 ms | 6,205 | $0.0148 | 0.00 |
| Multi-Agent Pipeline | 56,791 ms | 47,602 | $0.0778 | 0.67 |

## Interpretation

- Multi-agent token overhead: +667.2%.
- Multi-agent latency overhead: +323.3%.
- Multi-agent estimated-cost overhead: +426.3%.
- Average output length: 843 baseline words versus 935 multi-agent words.
- Circuit-breaker outcomes: 0/3 runs completed with unresolved errors flagged.
- Quality is intentionally left to the paired human-review output; factual-marker density is a descriptive proxy, not a quality score.

## Resume-Ready Bullets

- Architected a non-linear multi-agent content pipeline with Editor and Fact-Checker critique loops, durable feedback state, and a three-iteration circuit breaker that prevents unbounded token spend.
- Benchmarked the pipeline against a one-shot baseline across 3 seed topics, measuring 15,867 average tokens and $0.0259 estimated model cost per task.
- Implemented per-agent production telemetry for latency, prompt/completion tokens, model-aware cost, and end-to-end runs; quantified +667.2% token and +323.3% latency overhead versus the baseline.

## Per-Topic Results

| Topic | Baseline Tokens | Pipeline Tokens | Pipeline Revisions | Pipeline Cost | Status |
|---|---:|---:|---:|---:|---|
| Why idempotency is the hidden foundation of reliable distributed systems | 2,160 | 16,229 | 1 | $0.0269 | completed |
| How small teams can make better decisions under uncertainty | 2,066 | 10,869 | 0 | $0.0177 | completed |
| A practical guide to designing APIs that are easy to evolve | 1,979 | 20,504 | 1 | $0.0333 | completed |
