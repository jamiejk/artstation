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


if __name__ == "__main__":
    unittest.main()
