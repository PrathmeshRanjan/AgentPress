from __future__ import annotations

import unittest
from fastapi.testclient import TestClient

from app import app


class BenchmarkEndpointTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_benchmark_summary_endpoint(self):
        response = self.client.get("/api/benchmark/summary")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data.get("available"))
        self.assertEqual(data.get("total_runs"), 3)
        self.assertIn("aggregate", data)
        self.assertIn("baseline", data["aggregate"])
        self.assertIn("pipeline", data["aggregate"])
        self.assertIn("overhead", data["aggregate"])
        self.assertAlmostEqual(data["aggregate"]["overhead"]["token_overhead_percent"], 667.2, places=1)
        self.assertEqual(len(data.get("topics", [])), 3)

    def test_benchmark_topics_endpoint(self):
        response = self.client.get("/api/benchmark/topics")
        self.assertEqual(response.status_code, 200)
        topics = response.json()
        self.assertEqual(len(topics), 3)
        for t in topics:
            self.assertIn("topic", t)
            self.assertIn("system_a", t)
            self.assertIn("system_b", t)
            self.assertIn("pipeline_invocations", t)
            self.assertIn("identity", t["system_a"])
            self.assertIn("identity", t["system_b"])
            self.assertTrue(len(t["pipeline_invocations"]) >= 10)

    def test_benchmark_report_endpoint(self):
        # HTML browser view
        res_html = self.client.get("/BENCHMARK_REPORT.md", headers={"accept": "text/html"})
        self.assertEqual(res_html.status_code, 200)
        self.assertIn("AgentPress - Benchmark Report", res_html.text)

        # Raw markdown view
        res_raw = self.client.get("/BENCHMARK_REPORT.md?raw=true")
        self.assertEqual(res_raw.status_code, 200)
        self.assertIn("# AgentPress Benchmark Report", res_raw.text)


if __name__ == "__main__":
    unittest.main()
