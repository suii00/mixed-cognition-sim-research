import unittest

from tools.compare_vllm_dual_worker import compare_schedules


class CompareVllmDualWorkerTests(unittest.TestCase):
    def test_schedule_comparison_uses_two_sequential_baseline_runs(self):
        single = {
            "run_id": "single",
            "wall_seconds": 400.0,
            "logical_llm_calls": 2880,
            "failure_events": 0,
            "sampled_aggregate_prompt_tokens_per_second": 100.0,
            "sampled_aggregate_generation_tokens_per_second": 50.0,
        }
        dual = {
            "wall_seconds": 410.0,
            "logical_llm_calls": 5760,
            "logical_calls_per_second": 5760 / 410.0,
            "failure_events": 0,
            "failure_events_per_logical_call": 0.0,
            "sampled_aggregate_prompt_tokens_per_second": 195.0,
            "sampled_aggregate_generation_tokens_per_second": 98.0,
            "gpu_seconds": 6 * 410.0,
            "logical_calls_per_gpu_second": 5760 / (6 * 410.0),
        }
        report = compare_schedules(single, dual)
        baseline = report["two_run_sequential_baseline"]
        comparison = report["comparison"]
        self.assertEqual(baseline["wall_seconds"], 800.0)
        self.assertEqual(baseline["logical_llm_calls"], 5760)
        self.assertAlmostEqual(comparison["wall_speedup"], 800 / 410)
        self.assertAlmostEqual(comparison["logical_throughput_ratio"], 800 / 410)
        self.assertAlmostEqual(comparison["gpu_seconds_ratio"], 410 / 400)

    def test_sixty_run_projection_uses_thirty_dual_waves(self):
        single = {
            "run_id": "single",
            "wall_seconds": 360.0,
            "logical_llm_calls": 2880,
            "failure_events": 0,
            "sampled_aggregate_prompt_tokens_per_second": 100.0,
            "sampled_aggregate_generation_tokens_per_second": 50.0,
        }
        dual = {
            "wall_seconds": 370.0,
            "logical_llm_calls": 5760,
            "logical_calls_per_second": 5760 / 370.0,
            "failure_events": 0,
            "failure_events_per_logical_call": 0.0,
            "sampled_aggregate_prompt_tokens_per_second": 190.0,
            "sampled_aggregate_generation_tokens_per_second": 95.0,
            "gpu_seconds": 6 * 370.0,
            "logical_calls_per_gpu_second": 5760 / (6 * 370.0),
        }
        projection = compare_schedules(single, dual)["projection"]
        self.assertEqual(projection["two_worker_wave_count"], 30)
        self.assertEqual(projection["one_worker_sequential_hours"], 6.0)
        self.assertAlmostEqual(projection["two_worker_hours"], 30 * 370 / 3600)


if __name__ == "__main__":
    unittest.main()
