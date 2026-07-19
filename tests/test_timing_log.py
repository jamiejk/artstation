import io
import unittest
from unittest import mock

from server import timing_log


class TimingLogTests(unittest.TestCase):
    def test_write_timing_emits_structured_line_and_result(self):
        log = io.StringIO()

        with mock.patch.object(timing_log.time, "monotonic", side_effect=[10.0, 10.1234]):
            start = timing_log.monotonic()
            result = timing_log.write_timing(log, "sample", start, job_id="job", ignored=None)

        self.assertIn("[timing] sample elapsed_ms=123.4 job_id=job", log.getvalue())
        self.assertEqual(result["event"], "sample")
        self.assertEqual(result["elapsed_ms"], 123.4)
        self.assertEqual(result["job_id"], "job")
        self.assertNotIn("ignored", result)

    def test_parse_and_summarize_timing_lines(self):
        text = "\n".join(
            [
                "ordinary log line",
                "[timing] dip_travel_to_well elapsed_ms=1200.5 job_id=abc layer=1",
                "[timing] axicli_layer_resume elapsed_ms=2500.0 job_id=abc resume=True",
                "[timing] dip_travel_to_well elapsed_ms=300.0 job_id=abc",
            ]
        )

        events = timing_log.parse_timing_lines(text)
        summary = timing_log.summarize_timing_events(events, limit=2)

        self.assertEqual([event["event"] for event in events], ["dip_travel_to_well", "axicli_layer_resume", "dip_travel_to_well"])
        self.assertEqual(events[1]["resume"], True)
        self.assertEqual(summary["total_elapsed_ms"], 4000.5)
        self.assertEqual(summary["totals"]["dip_travel_to_well"], {"count": 2, "elapsed_ms": 1500.5, "max_elapsed_ms": 1200.5})
        self.assertEqual(summary["slowest"][0]["event"], "axicli_layer_resume")


if __name__ == "__main__":
    unittest.main()
