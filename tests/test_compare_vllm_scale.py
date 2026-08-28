import unittest
from datetime import datetime, timezone

from tools.compare_vllm_scale import compare, parse_throughput_samples, summarize


class CompareVllmScaleTests(unittest.TestCase):
    def test_samples_are_bounded_to_workload_interval(self):
        start = datetime(2026, 8, 23, 10, 0, 5, tzinfo=timezone.utc)
        end = datetime(2026, 8, 23, 10, 0, 25, tzinfo=timezone.utc)
        lines = [
            "INFO 08-23 10:00:04 Avg prompt throughput: 1.0 tokens/s, "
            "Avg generation throughput: 2.0 tokens/s",
            "INFO 08-23 10:00:15 Avg prompt throughput: 3.0 tokens/s, "
            "Avg generation throughput: 4.0 tokens/s",
            "INFO 08-23 10:00:26 Avg prompt throughput: 5.0 tokens/s, "
            "Avg generation throughput: 6.0 tokens/s",
        ]
        samples = parse_throughput_samples(lines, start, end)
        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0]["prompt_tokens_per_second"], 3.0)
        self.assertEqual(samples[0]["generation_tokens_per_second"], 4.0)

    def test_summary_retains_sample_distribution(self):
        self.assertEqual(summarize([1.0, 3.0]), {
            "sample_count": 2,
            "mean": 2.0,
            "median": 2.0,
            "minimum": 1.0,
            "maximum": 3.0,
        })

    def test_comparison_uses_declared_ratios(self):
        baseline = {
            "wall_seconds": 400.0,
            "logical_calls_per_second": 8.0,
            "sampled_aggregate_prompt_tokens_per_second": 100.0,
            "sampled_aggregate_generation_tokens_per_second": 50.0,
            "failure_events_per_logical_call": 0.0,
        }
        candidate = {
            "wall_seconds": 200.0,
            "logical_calls_per_second": 16.0,
            "sampled_aggregate_prompt_tokens_per_second": 150.0,
            "sampled_aggregate_generation_tokens_per_second": 125.0,
            "failure_events_per_logical_call": 0.0,
        }
        result = compare(baseline, candidate)["comparison"]
        self.assertEqual(result["wall_speedup"], 2.0)
        self.assertEqual(result["wall_reduction_percent"], 50.0)
        self.assertEqual(result["logical_throughput_ratio"], 2.0)
        self.assertEqual(result["sampled_prompt_throughput_ratio"], 1.5)
        self.assertEqual(result["sampled_generation_throughput_ratio"], 2.5)


if __name__ == "__main__":
    unittest.main()
