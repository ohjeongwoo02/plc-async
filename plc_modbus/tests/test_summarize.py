"""로그 요약 도구 테스트. 기대값은 손으로 계산했다."""
import unittest

from tools.summarize_log import summarize


def event(event_type, elapsed, **fields):
    return {"type": event_type, "elapsed": elapsed, **fields}


class SummaryTests(unittest.TestCase):
    def test_summary_fields(self):
        summary = summarize([
            event("started", 0.0, mode="sync"),
            event("heartbeat", 0.0),
            event("comm_start", 0.1, kind="read", station=1),
            event("comm_end", 0.15, kind="read", station=1, duration=0.05),
            event("observation", 0.15, value=7),
            event("heartbeat", 0.5),
            event("comm_start", 1.0, kind="read", station=2),
            event("comm_end", 3.0, kind="read", station=2, duration=2.0),
            event("read_failed", 3.0, station=2),
            event("key_seen", 3.01, key="s"),
            event("request_accepted", 3.02, command="write", value=1),
            event("heartbeat", 3.02),
        ])
        self.assertEqual(summary["mode"], "sync")
        self.assertEqual(summary["observations"], 1)
        self.assertEqual(summary["read_failures"], 1)
        self.assertAlmostEqual(summary["heartbeat_max_gap"], 2.52)
        self.assertEqual(len(summary["waits"]), 1)
        wait = summary["waits"][0]
        self.assertEqual((wait["kind"], wait["station"], wait["start"], wait["duration"]), ("read", 2, 1.0, 2.0))
        self.assertEqual((wait["keys_during"], wait["accepted_during"], wait["heartbeats_during"]), (0, 0, 0))
        self.assertAlmostEqual(wait["first_key_after_return"], 0.01)

    def test_activity_during_wait(self):
        summary = summarize([
            event("started", 0.0, mode="async"),
            event("comm_start", 1.0, kind="read", station=2),
            event("key_seen", 1.1, key="s"),
            event("status_shown", 1.2),
            event("request_accepted", 1.3),
            event("heartbeat", 1.5),
            event("comm_end", 3.0, kind="read", station=2, duration=2.0),
        ])
        wait = summary["waits"][0]
        self.assertEqual((wait["keys_during"], wait["accepted_during"], wait["heartbeats_during"]), (1, 1, 1))
        self.assertEqual(wait["status_during"], 1)
        self.assertIsNone(wait["first_key_after_return"])

    def test_interrupted_comm_not_a_wait(self):
        summary = summarize([event("started", 0.0, mode="async"), event("comm_start", 1.0, kind="read", station=2)])
        self.assertEqual(summary["waits"], [])
        self.assertEqual(summary["heartbeat_max_gap"], 0)


if __name__ == "__main__":
    unittest.main()
