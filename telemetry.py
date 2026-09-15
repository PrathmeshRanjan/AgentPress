"""Provider-neutral telemetry helpers for AgentPress model invocations."""

from __future__ import annotations

from dataclasses import dataclass
from time import perf_counter
from typing import Any, Mapping, Sequence


# Standard, paid-tier text pricing in USD per one million tokens. Keep aliases
# here because providers frequently return a dated model id in response metadata.
# Override these values at deployment time by changing the configured table.
MODEL_PRICING_USD_PER_MILLION: dict[str, dict[str, float]] = {
    "gemini-2.5-flash-image": {"input": 0.30, "output": 30.00},
    "mistral-small-latest": {"input": 0.15, "output": 0.60},
    "mistral-small": {"input": 0.15, "output": 0.60},
    "mistral-small-2603": {"input": 0.15, "output": 0.60},
    "gemini-2.5-flash": {"input": 0.30, "output": 2.50},
}


@dataclass(frozen=True)
class InvocationResult:
    """The model result paired with its serializable telemetry event."""

    value: Any
    event: dict[str, Any]


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _first_int(*values: Any) -> int:
    for value in values:
        if value is None:
            continue
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            continue
    return 0


def extract_usage(message: Any) -> tuple[int, int]:
    """Normalize token usage emitted by LangChain's supported providers."""

    usage = _as_mapping(getattr(message, "usage_metadata", None))
    metadata = _as_mapping(getattr(message, "response_metadata", None))
    token_usage = _as_mapping(
        metadata.get("token_usage")
        or metadata.get("usage")
        or metadata.get("usage_metadata")
    )

    prompt_tokens = _first_int(
        usage.get("input_tokens"),
        usage.get("prompt_tokens"),
        token_usage.get("prompt_tokens"),
        token_usage.get("input_tokens"),
        token_usage.get("prompt_token_count"),
    )
    completion_tokens = _first_int(
        usage.get("output_tokens"),
        usage.get("completion_tokens"),
        token_usage.get("completion_tokens"),
        token_usage.get("output_tokens"),
        token_usage.get("candidates_token_count"),
    )
    return prompt_tokens, completion_tokens


def extract_model_name(message: Any, configured_model: str) -> str:
    """Prefer the provider-reported model so fallback calls are priced correctly."""

    metadata = _as_mapping(getattr(message, "response_metadata", None))
    for key in ("model_name", "model", "model_id"):
        value = metadata.get(key)
        if value:
            return str(value)
    return configured_model


def resolve_pricing(model_name: str) -> tuple[str | None, dict[str, float] | None]:
    normalized = model_name.lower()
    for alias in sorted(MODEL_PRICING_USD_PER_MILLION, key=len, reverse=True):
        if alias in normalized:
            return alias, MODEL_PRICING_USD_PER_MILLION[alias]
    return None, None


def estimate_cost_usd(model_name: str, prompt_tokens: int, completion_tokens: int) -> tuple[float, str]:
    """Return estimated token cost and whether a matching price was available."""

    _, pricing = resolve_pricing(model_name)
    if pricing is None:
        return 0.0, "pricing_unavailable"
    cost = (
        prompt_tokens * pricing["input"]
        + completion_tokens * pricing["output"]
    ) / 1_000_000
    return round(cost, 8), "estimated"


def make_event(
    *,
    agent: str,
    started_at: float,
    message: Any | None = None,
    configured_model: str = "",
    status: str = "success",
    error: str | None = None,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
) -> dict[str, Any]:
    """Build a stable, JSON-compatible event for one agent/node invocation."""

    measured_prompt, measured_completion = extract_usage(message)
    input_tokens = measured_prompt if prompt_tokens is None else prompt_tokens
    output_tokens = measured_completion if completion_tokens is None else completion_tokens
    model_name = extract_model_name(message, configured_model) if message is not None else configured_model
    cost, cost_status = estimate_cost_usd(model_name, input_tokens, output_tokens)
    event: dict[str, Any] = {
        "agent": agent,
        "model": model_name or "none",
        "latency_ms": round((perf_counter() - started_at) * 1000, 2),
        "prompt_tokens": input_tokens,
        "completion_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "estimated_cost_usd": cost,
        "cost_status": cost_status if model_name else "not_applicable",
        "status": status,
    }
    if error:
        event["error"] = error
    return event


def invoke_model(
    *,
    agent: str,
    runnable: Any,
    messages: Sequence[Any],
    configured_model: str,
) -> InvocationResult:
    """Invoke a normal chat model and capture latency, usage, and cost."""

    started_at = perf_counter()
    try:
        message = runnable.invoke(list(messages))
    except Exception as exc:
        # The exception is re-raised so LangGraph retains its normal failure
        # semantics. Successful fallback calls are recorded from their response.
        raise RuntimeError(f"{agent} invocation failed: {exc}") from exc
    return InvocationResult(
        value=message,
        event=make_event(
            agent=agent,
            started_at=started_at,
            message=message,
            configured_model=configured_model,
        ),
    )


def invoke_structured_model(
    *,
    agent: str,
    runnable: Any,
    schema: Any,
    messages: Sequence[Any],
    configured_model: str,
) -> InvocationResult:
    """Invoke a structured model while retaining the raw message usage metadata."""

    started_at = perf_counter()
    structured = runnable.with_structured_output(schema, include_raw=True)
    result = structured.invoke(list(messages))
    raw = result.get("raw") if isinstance(result, Mapping) else None
    parsed = result.get("parsed") if isinstance(result, Mapping) else result
    parsing_error = result.get("parsing_error") if isinstance(result, Mapping) else None
    if parsing_error or parsed is None:
        raise ValueError(f"{agent} returned invalid structured output: {parsing_error}")
    return InvocationResult(
        value=parsed,
        event=make_event(
            agent=agent,
            started_at=started_at,
            message=raw,
            configured_model=configured_model,
        ),
    )


def summarize_telemetry(
    events: Sequence[Mapping[str, Any]],
    *,
    pipeline_latency_ms: float,
    revision_count: int,
) -> dict[str, Any]:
    """Aggregate per-invocation records without losing their raw detail."""

    prompt_tokens = sum(_first_int(event.get("prompt_tokens")) for event in events)
    completion_tokens = sum(_first_int(event.get("completion_tokens")) for event in events)
    return {
        "latency_ms": round(pipeline_latency_ms, 2),
        "agent_latency_ms_sum": round(
            sum(float(event.get("latency_ms", 0.0) or 0.0) for event in events),
            2,
        ),
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
        "estimated_cost_usd": round(
            sum(float(event.get("estimated_cost_usd", 0.0) or 0.0) for event in events),
            8,
        ),
        "revision_loop_count": revision_count,
        "agent_invocation_count": len(events),
        "invocations": list(events),
    }
