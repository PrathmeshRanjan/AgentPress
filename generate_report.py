"""Generate BENCHMARK_REPORT.md from AgentPress benchmark JSONL telemetry."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from statistics import mean
from typing import Any


def _load_records(path: Path) -> list[dict[str, Any]]:
    records = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON on {path}:{line_number}: {exc}") from exc
        if record.get("status") == "completed":
            records.append(record)
    if not records:
        raise ValueError(f"No completed benchmark records found in {path}.")
    return records


def _aggregate(records: list[dict[str, Any]], system: str) -> dict[str, float]:
    telemetry = [record[system]["telemetry"] for record in records]
    return {
        "average_latency_ms": mean(float(item["latency_ms"]) for item in telemetry),
        "total_tokens": sum(int(item["total_tokens"]) for item in telemetry),
        "total_cost_usd": sum(float(item["estimated_cost_usd"]) for item in telemetry),
        "average_revision_loops": mean(float(item.get("revision_loop_count", 0)) for item in telemetry),
        "average_cost_usd": mean(float(item["estimated_cost_usd"]) for item in telemetry),
        "average_tokens": mean(float(item["total_tokens"]) for item in telemetry),
    }


def _percent_change(new: float, old: float) -> float:
    return 0.0 if old == 0 else (new / old - 1) * 100


def generate_report(records: list[dict[str, Any]]) -> str:
    baseline = _aggregate(records, "baseline")
    pipeline = _aggregate(records, "pipeline")
    token_overhead = _percent_change(pipeline["average_tokens"], baseline["average_tokens"])
    latency_overhead = _percent_change(pipeline["average_latency_ms"], baseline["average_latency_ms"])
    unresolved = sum(
        bool(record["pipeline"]["metadata"].get("unresolved_errors", False))
        for record in records
    )

    topic_label = "seed topic" if len(records) == 1 else "seed topics"
    bullets = [
        "Architected a non-linear multi-agent content pipeline with Editor and Fact-Checker critique loops, durable feedback state, and a three-iteration circuit breaker that prevents unbounded token spend.",
        (
            f"Benchmarked the pipeline against a one-shot baseline across {len(records)} {topic_label}, "
            f"measuring {pipeline['average_tokens']:,.0f} average tokens and "
            f"${pipeline['average_cost_usd']:.4f} estimated model cost per task."
        ),
        (
            f"Implemented per-agent production telemetry for latency, prompt/completion tokens, model-aware cost, "
            f"and end-to-end runs; quantified {token_overhead:+.1f}% token and "
            f"{latency_overhead:+.1f}% latency overhead versus the baseline."
        ),
    ]

    lines = [
        "# AgentPress Benchmark Report",
        "",
        f"Generated from {len(records)} completed paired runs. Image generation was disabled to isolate text orchestration.",
        *(
            [
                "",
                f"> Benchmark status: partial ({len(records)}/5 default seed topics). Provider daily quotas prevented completion of the remaining paired runs; use `python benchmark.py --resume` after quota reset.",
            ]
            if len(records) < 5
            else []
        ),
        "",
        "## Aggregate Results",
        "",
        "| System | Average Latency | Total Tokens | Total Estimated Cost | Average Revision Loops |",
        "|---|---:|---:|---:|---:|",
        (
            f"| Vanilla Baseline | {baseline['average_latency_ms']:,.0f} ms | "
            f"{baseline['total_tokens']:,.0f} | ${baseline['total_cost_usd']:.4f} | "
            f"{baseline['average_revision_loops']:.2f} |"
        ),
        (
            f"| Multi-Agent Pipeline | {pipeline['average_latency_ms']:,.0f} ms | "
            f"{pipeline['total_tokens']:,.0f} | ${pipeline['total_cost_usd']:.4f} | "
            f"{pipeline['average_revision_loops']:.2f} |"
        ),
        "",
        "## Interpretation",
        "",
        f"- Multi-agent token overhead: {token_overhead:+.1f}%.",
        f"- Multi-agent latency overhead: {latency_overhead:+.1f}%.",
        f"- Circuit-breaker outcomes: {unresolved}/{len(records)} runs completed with unresolved errors flagged.",
        "- Quality is intentionally left to the paired human-review output; factual-marker density is a descriptive proxy, not a quality score.",
        "",
        "## Resume-Ready Bullets",
        "",
        *[f"- {bullet}" for bullet in bullets],
        "",
        "## Per-Topic Results",
        "",
        "| Topic | Baseline Tokens | Pipeline Tokens | Pipeline Revisions | Pipeline Cost | Status |",
        "|---|---:|---:|---:|---:|---|",
    ]
    for record in records:
        topic = str(record["topic"]).replace("|", "\\|")
        baseline_t = record["baseline"]["telemetry"]
        pipeline_t = record["pipeline"]["telemetry"]
        status = record["pipeline"]["metadata"].get("status", "completed")
        lines.append(
            f"| {topic} | {baseline_t['total_tokens']:,} | {pipeline_t['total_tokens']:,} | "
            f"{pipeline_t.get('revision_loop_count', 0)} | ${pipeline_t['estimated_cost_usd']:.4f} | {status} |"
        )
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=Path("benchmark_results.jsonl"))
    parser.add_argument("--output", type=Path, default=Path("BENCHMARK_REPORT.md"))
    parser.add_argument(
        "--clean-output",
        type=Path,
        help="Optionally write only completed input records to a normalized JSONL file.",
    )
    args = parser.parse_args()
    records = _load_records(args.input)
    report = generate_report(records)
    args.output.write_text(report, encoding="utf-8")
    if args.clean_output:
        args.clean_output.write_text(
            "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
            encoding="utf-8",
        )
    print(f"Wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
