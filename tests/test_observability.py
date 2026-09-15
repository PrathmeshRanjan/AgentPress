from __future__ import annotations

import unittest

from backend import (
    MAX_REVISIONS,
    route_after_editor,
    route_after_fact_checker,
)
from telemetry import estimate_cost_usd, extract_usage, summarize_telemetry


class FakeMessage:
    usage_metadata = {"input_tokens": 1000, "output_tokens": 500}
    response_metadata = {"model_name": "gemini-2.5-flash"}


class TelemetryTests(unittest.TestCase):
    def test_provider_usage_and_cost_are_normalized(self):
        self.assertEqual(extract_usage(FakeMessage()), (1000, 500))
        cost, status = estimate_cost_usd("gemini-2.5-flash", 1000, 500)
        self.assertEqual(status, "estimated")
        self.assertAlmostEqual(cost, 0.00155)

    def test_summary_keeps_per_agent_events(self):
        event = {
            "agent": "writer",
            "latency_ms": 10,
            "prompt_tokens": 12,
            "completion_tokens": 8,
            "estimated_cost_usd": 0.01,
        }
        summary = summarize_telemetry(
            [event], pipeline_latency_ms=12.5, revision_count=2
        )
        self.assertEqual(summary["total_tokens"], 20)
        self.assertEqual(summary["revision_loop_count"], 2)
        self.assertEqual(summary["invocations"], [event])


class CircuitBreakerTests(unittest.TestCase):
    def test_editor_rejection_routes_to_writer_before_limit(self):
        self.assertEqual(
            route_after_editor({"editor_approved": False, "revision_count": 2}),
            "revision_writer",
        )

    def test_editor_rejection_trips_breaker_at_limit(self):
        self.assertEqual(
            route_after_editor(
                {"editor_approved": False, "revision_count": MAX_REVISIONS}
            ),
            "unresolved",
        )

    def test_fact_checker_approval_advances(self):
        self.assertEqual(
            route_after_fact_checker(
                {"fact_checker_approved": True, "revision_count": MAX_REVISIONS}
            ),
            "approved",
        )

    def test_fact_checker_rejection_trips_breaker_at_limit(self):
        self.assertEqual(
            route_after_fact_checker(
                {"fact_checker_approved": False, "revision_count": MAX_REVISIONS}
            ),
            "unresolved",
        )


if __name__ == "__main__":
    unittest.main()
