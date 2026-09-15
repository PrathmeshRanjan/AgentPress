"""Benchmark AgentPress against a single-call, zero-shot writing baseline."""

from __future__ import annotations

import argparse
import html
import json
import re
import time
import uuid
from pathlib import Path
from time import perf_counter
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from backend import PRIMARY_MODEL_NAME, model, workflow
from telemetry import invoke_model, summarize_telemetry


DEFAULT_TOPICS = [
    "Why idempotency is the hidden foundation of reliable distributed systems",
    "How small teams can make better decisions under uncertainty",
    "A practical guide to designing APIs that are easy to evolve",
    "Why deliberate practice works and where people apply it incorrectly",
    "The trade-offs between synchronous and asynchronous collaboration",
]

BASELINE_SYSTEM = """You are an expert researcher, writer, editor, and fact-checker working alone.
Create a publication-ready long-form Markdown article in one response. Use a compelling H1 title,
clear H2 sections, concrete examples, nuanced trade-offs, and a concise ending. Write 1,500-2,000
words in a natural expert voice. Do not invent sources, links, studies, statistics, or quotations.
Output only the finished article."""


def _text_metrics(content: str) -> dict[str, float | int]:
    words = re.findall(r"\b\w+\b", content)
    denominator = max(len(words), 1) / 1000
    citations = len(re.findall(r"\[[^\]]+\]\(https?://[^)]+\)", content))
    numeric_markers = len(re.findall(r"(?<!\w)(?:\d+(?:\.\d+)?%?|\d{4})(?!\w)", content))
    return {
        "word_count": len(words),
        "citations": citations,
        "factual_markers_per_1k_words": round((citations + numeric_markers) / denominator, 2),
    }


def _is_transient_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(marker in message for marker in ("429", "rate limit", "timeout", "temporarily unavailable"))


def run_baseline(topic: str, *, retries: int, retry_delay: float) -> dict[str, Any]:
    started_at = perf_counter()
    for attempt in range(retries + 1):
        try:
            invocation = invoke_model(
                agent="vanilla_baseline",
                runnable=model,
                configured_model=PRIMARY_MODEL_NAME,
                messages=[
                    SystemMessage(content=BASELINE_SYSTEM),
                    HumanMessage(content=f"Topic: {topic}"),
                ],
            )
            break
        except Exception as exc:
            if attempt >= retries or not _is_transient_error(exc):
                raise
            wait_seconds = retry_delay * (attempt + 1)
            print(f"  baseline throttled; retrying in {wait_seconds:.0f}s", flush=True)
            time.sleep(wait_seconds)
    content = invocation.value.content.strip()
    telemetry = summarize_telemetry(
        [invocation.event],
        pipeline_latency_ms=(perf_counter() - started_at) * 1000,
        revision_count=0,
    )
    return {
        "content": content,
        "metadata": {"status": "completed", "revision_count": 0},
        "telemetry": telemetry,
        "text_metrics": _text_metrics(content),
    }


def run_multi_agent(
    topic: str,
    *,
    retries: int,
    retry_delay: float,
    max_concurrency: int,
) -> dict[str, Any]:
    run_id = f"benchmark-{uuid.uuid4().hex}"
    initial_input: dict[str, Any] | None = {
        "topic": topic,
        "sections": [],
        "feedback_history": [],
        "telemetry_events": [],
        "revision_count": 0,
        "pipeline_started_at": perf_counter(),
        # Image generation is excluded so this benchmark isolates the text
        # orchestration overhead and remains directly comparable.
        "enable_images": False,
    }
    config = {
        "configurable": {"thread_id": run_id},
        "recursion_limit": 50,
        "max_concurrency": max_concurrency,
    }
    for attempt in range(retries + 1):
        try:
            result_state = workflow.invoke(initial_input, config=config)
            break
        except Exception as exc:
            if attempt >= retries or not _is_transient_error(exc):
                raise
            wait_seconds = retry_delay * (attempt + 1)
            print(f"  pipeline throttled; resuming checkpoint in {wait_seconds:.0f}s", flush=True)
            time.sleep(wait_seconds)
            # Passing None resumes the same checkpoint and retries only unfinished work.
            initial_input = None
    result = dict(result_state["result"])
    result["text_metrics"] = _text_metrics(result["content"])
    return result


def _comparison(baseline: dict[str, Any], pipeline: dict[str, Any]) -> dict[str, float]:
    baseline_telemetry = baseline["telemetry"]
    pipeline_telemetry = pipeline["telemetry"]
    baseline_tokens = max(int(baseline_telemetry["total_tokens"]), 1)
    baseline_cost = max(float(baseline_telemetry["estimated_cost_usd"]), 0.00000001)
    return {
        "token_overhead_percent": round(
            (pipeline_telemetry["total_tokens"] / baseline_tokens - 1) * 100,
            2,
        ),
        "latency_overhead_percent": round(
            (pipeline_telemetry["latency_ms"] / max(baseline_telemetry["latency_ms"], 0.01) - 1) * 100,
            2,
        ),
        "cost_overhead_percent": round(
            (pipeline_telemetry["estimated_cost_usd"] / baseline_cost - 1) * 100,
            2,
        ),
    }


def write_human_review(records: list[dict[str, Any]], path: Path) -> None:
    lines = [
        "# Baseline vs. Multi-Agent Output Review",
        "",
        "The complete outputs are shown side-by-side for blind human quality review.",
        "Images are disabled in both paths; the baseline receives no web research or revision loop.",
        "",
    ]
    for index, record in enumerate(records, start=1):
        baseline = html.escape(record["baseline"]["content"])
        pipeline = html.escape(record["pipeline"]["content"])
        lines.extend(
            [
                f"## {index}. {record['topic']}",
                "",
                "<table><thead><tr><th width=\"50%\">Vanilla baseline</th><th width=\"50%\">Multi-agent pipeline</th></tr></thead>",
                f"<tbody><tr><td valign=\"top\"><pre>{baseline}</pre></td><td valign=\"top\"><pre>{pipeline}</pre></td></tr></tbody></table>",
                "",
            ]
        )
    path.write_text("\n".join(lines), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--topics-file", type=Path, help="Optional newline-delimited topic file.")
    parser.add_argument("--limit", type=int, default=None, help="Run only the first N topics.")
    parser.add_argument("--output", type=Path, default=Path("benchmark_results.jsonl"))
    parser.add_argument("--review-output", type=Path, default=Path("BENCHMARK_OUTPUTS.md"))
    parser.add_argument("--max-concurrency", type=int, default=3)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--retry-delay", type=float, default=30.0)
    parser.add_argument("--cooldown-seconds", type=float, default=20.0)
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Keep completed records in --output and run only unfinished topics.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    topics = DEFAULT_TOPICS
    if args.topics_file:
        topics = [line.strip() for line in args.topics_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    if args.limit is not None:
        topics = topics[: max(args.limit, 0)]
    if not topics:
        raise SystemExit("No benchmark topics were supplied.")

    records: list[dict[str, Any]] = []
    if args.resume and args.output.exists():
        for line in args.output.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            if record.get("status") == "completed":
                records.append(record)
        completed_topics = {record["topic"] for record in records}
        topics = [topic for topic in topics if topic not in completed_topics]

    args.output.parent.mkdir(parents=True, exist_ok=True)
    output_mode = "a" if args.resume and args.output.exists() else "w"
    with args.output.open(output_mode, encoding="utf-8") as handle:
        for index, topic in enumerate(topics, start=1):
            print(f"[{index}/{len(topics)}] {topic}", flush=True)
            started_at = perf_counter()
            try:
                baseline = run_baseline(
                    topic,
                    retries=max(args.retries, 0),
                    retry_delay=max(args.retry_delay, 0),
                )
                pipeline = run_multi_agent(
                    topic,
                    retries=max(args.retries, 0),
                    retry_delay=max(args.retry_delay, 0),
                    max_concurrency=max(args.max_concurrency, 1),
                )
                record = {
                    "topic": topic,
                    "status": "completed",
                    "baseline": baseline,
                    "pipeline": pipeline,
                    "comparison": _comparison(baseline, pipeline),
                    "benchmark_wall_time_ms": round((perf_counter() - started_at) * 1000, 2),
                }
            except Exception as exc:
                record = {
                    "topic": topic,
                    "status": "failed",
                    "error": str(exc),
                    "benchmark_wall_time_ms": round((perf_counter() - started_at) * 1000, 2),
                }
            records.append(record)
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            handle.flush()
            if index < len(topics) and args.cooldown_seconds > 0:
                time.sleep(args.cooldown_seconds)

    completed = [record for record in records if record["status"] == "completed"]
    if completed:
        write_human_review(completed, args.review_output)
    print(f"Completed {len(completed)}/{len(records)} topics. Results: {args.output}", flush=True)
    return 0 if len(completed) == len(records) else 1


if __name__ == "__main__":
    raise SystemExit(main())
