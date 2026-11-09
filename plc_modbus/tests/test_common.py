"""plc_common.py 단위 테스트. 통신 없이 판단 로직만 확인한다."""
import io
import json
from pathlib import Path
import tempfile
import unittest

import plc_common
from plc_common import Monitor, parse_command


class FakeClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class FakeReply:
    def __init__(self, registers=(), error=False, function_code=3):
        self.registers = list(registers)
        self.error = error
        self.exception_code = 2
        self.function_code = function_code

    def isError(self):
        return self.error


class FakeKeys:
    def __init__(self, text):
        self.chars = list(text)

    def kbhit(self):
        return bool(self.chars)

    def getwch(self):
        return self.chars.pop(0)


def make_monitor(**over):
    clock = FakeClock()
    tmp = tempfile.TemporaryDirectory()
    log = plc_common.EventLog(Path(tmp.name) / "t.jsonl", stream=io.StringIO(), clock=clock, keep_events=True)
    config = plc_common.Config(**{**plc_common.Config.defaults(), **over})
    monitor = Monitor(config, log, clock=clock)
    unittest.addModuleCleanup(tmp.cleanup)
    unittest.addModuleCleanup(log.close)
    return monitor, log, clock


def of_type(log, kind):
    return [e for e in log.events if e["type"] == kind]


def finish(monitor, comm, ok=True, value=None):
    if comm.kind == "write":
        monitor.finish_write(comm, ok, None if ok else "무응답")
    elif ok:
        monitor.finish_read(comm, value=value)
    else:
        monitor.finish_read(comm, error="무응답")


class ConfigTests(unittest.TestCase):
    def test_config_defaults(self):
        path = Path(plc_common.__file__).with_name("plc_config.json")
        config = plc_common.load_config(path)
        self.assertEqual(
            (config.port, config.baudrate, config.bytesize, config.parity, config.stopbits,
             config.station, config.timeout, config.retries, config.poll_interval),
            ("COM5", 9600, 8, "N", 1, 1, 2.0, 0, 1.0),
        )
        self.assertEqual(plc_common.parse_args([]).station, 1)
        self.assertEqual(plc_common.parse_args(["--station", "2"]).station, 2)

    def test_client_kwargs(self):
        config = plc_common.Config(**plc_common.Config.defaults())
        self.assertEqual(config.client_kwargs(), {
            "baudrate": 9600, "bytesize": 8, "parity": "N", "stopbits": 1, "timeout": 2.0, "retries": 0,
        })

    def test_config_rejects_station_zero(self):
        with self.assertRaises(ValueError):
            plc_common.Config(**{**plc_common.Config.defaults(), "station": 0})


class ParseTests(unittest.TestCase):
    def test_parse_valid(self):
        self.assertEqual(parse_command("write 0").value, 0)
        self.assertEqual(parse_command("write 65535").value, 65535)
        self.assertEqual(parse_command("  WRITE   100 ").value, 100)
        self.assertEqual(parse_command("station 2").kind, "station")
        self.assertEqual(parse_command("status").kind, "status")
        self.assertEqual(parse_command("quit").kind, "quit")

    def test_parse_rejects(self):
        for line in ("write 65536", "write -1", "write 1.5", "write", "write 1 2", "write １００",
                     "station 0", "station 3", "station", "status 1", "", "   ", "쓰기 100"):
            with self.subTest(line=line), self.assertRaises(ValueError):
                parse_command(line)


class KeyLinesTests(unittest.TestCase):
    def setUp(self):
        self.monitor, self.log, _ = make_monitor()

    def lines(self, text):
        return plc_common.KeyLines(self.log, FakeKeys(text)).poll()

    def test_keylines_edit(self):
        self.assertEqual(self.lines("wrx\x08ite 1\r"), ["write 1"])
        self.assertEqual(self.lines("a\rb\n"), ["a", "b"])
        self.assertEqual(self.lines("a\r\nb\r"), ["a", "b"])
        self.assertEqual(self.lines("\x00;a\xe0Hb\r"), ["ab"])
        self.assertEqual(self.lines("\x08\r"), [""])
        self.assertEqual(self.lines("wr\x03"), ["quit"])

    def test_partial_line_waits(self):
        keys = FakeKeys("wri")
        lines = plc_common.KeyLines(self.log, keys)
        self.assertEqual(lines.poll(), [])
        keys.chars.extend("te 1\r")
        self.assertEqual(lines.poll(), ["write 1"])

    def test_key_seen_recorded(self):
        self.lines("ab\r")
        self.assertEqual(len(of_type(self.log, "key_seen")), 3)


class TurnTests(unittest.TestCase):
    def test_first_turn_is_read(self):
        monitor, _, _ = make_monitor()
        self.assertEqual(monitor.next_comm().kind, "read")

    def test_turn_order(self):
        monitor, _, clock = make_monitor()
        finish(monitor, monitor.next_comm(), value=1)
        monitor.accept_line("write 100")
        monitor.accept_line("write 200")
        seen = []
        for _ in range(4):
            comm = monitor.next_comm()
            seen.append((comm.kind, comm.value))
            finish(monitor, comm, value=comm.value)
        self.assertEqual(seen, [("write", 100), ("read", None), ("write", 200), ("read", None)])
        self.assertIsNone(monitor.next_comm())

    def test_failed_write_no_readback(self):
        monitor, _, _ = make_monitor()
        finish(monitor, monitor.next_comm(), value=1)
        monitor.accept_line("write 100")
        monitor.accept_line("write 200")
        finish(monitor, monitor.next_comm(), ok=False)
        self.assertEqual(monitor.next_comm().value, 200)

    def test_read_not_starved(self):
        monitor, _, clock = make_monitor()
        finish(monitor, monitor.next_comm(), value=1)
        for value in (1, 2, 3):
            monitor.accept_line(f"write {value}")
        kinds = []
        for _ in range(5):
            clock.now += 2.0
            comm = monitor.next_comm()
            kinds.append(comm.kind)
            finish(monitor, comm, ok=False)
        self.assertEqual(kinds, ["write", "read", "write", "read", "write"])

    def test_period_no_catchup(self):
        monitor, log, clock = make_monitor()
        finish(monitor, monitor.next_comm(), value=1)
        self.assertIsNone(monitor.next_comm())
        clock.now += 5.0
        finish(monitor, monitor.next_comm(), value=1)
        self.assertIsNone(monitor.next_comm())
        clock.now += 1.0
        self.assertEqual(monitor.next_comm().kind, "read")

    def test_station_in_order(self):
        monitor, log, _ = make_monitor()
        finish(monitor, monitor.next_comm(), value=1)
        for line in ("write 100", "station 2", "write 200"):
            monitor.accept_line(line)
        first = monitor.next_comm()
        self.assertEqual((first.value, first.station), (100, 1))
        finish(monitor, first, ok=False)
        self.assertEqual(of_type(log, "station_changed"), [])
        second = monitor.next_comm()
        self.assertEqual((second.value, second.station), (200, 2))
        self.assertEqual(len(of_type(log, "station_changed")), 1)
        self.assertEqual(monitor.station, 2)

    def test_comm_start_end_recorded(self):
        monitor, log, clock = make_monitor()
        comm = monitor.next_comm()
        self.assertIs(monitor.in_flight, comm)
        clock.now += 0.25
        finish(monitor, comm, value=5)
        self.assertIsNone(monitor.in_flight)
        self.assertEqual(of_type(log, "comm_start")[0]["kind"], "read")
        self.assertAlmostEqual(of_type(log, "comm_end")[0]["duration"], 0.25, places=3)


class StateTests(unittest.TestCase):
    def test_last_good_kept(self):
        monitor, log, clock = make_monitor()
        finish(monitor, monitor.next_comm(), value=7)
        good = dict(monitor.last_observation)
        clock.now += 1.0
        finish(monitor, monitor.next_comm(), ok=False)
        self.assertEqual(monitor.last_observation, good)
        failed = of_type(log, "read_failed")[0]
        self.assertNotIn("value", failed)
        self.assertEqual(failed["reason"], "무응답")
        monitor.show_status()
        status = of_type(log, "status_shown")[0]
        self.assertEqual(status["observed_value"], 7)
        self.assertIn("실패", status["last_read"])

    def test_unobserved(self):
        monitor, log, _ = make_monitor(station=2)
        finish(monitor, monitor.next_comm(), ok=False)
        monitor.show_status()
        status = of_type(log, "status_shown")[0]
        self.assertEqual(status["observed"], "미관측")
        self.assertNotIn("observed_value", status)
        self.assertEqual(of_type(log, "observation"), [])

    def test_observation_matches(self):
        monitor, log, clock = make_monitor()
        finish(monitor, monitor.next_comm(), value=1)
        self.assertNotIn("expected", of_type(log, "observation")[0])
        for value, matches in ((100, True), (200, False)):
            monitor.accept_line(f"write {value}")
            finish(monitor, monitor.next_comm())
            finish(monitor, monitor.next_comm(), value=100)
            last = of_type(log, "observation")[-1]
            self.assertEqual((last["expected"], last["matches"]), (value, matches))
        clock.now += 1.0
        finish(monitor, monitor.next_comm(), value=100)
        self.assertNotIn("expected", of_type(log, "observation")[-1])

    def test_expected_cleared_by_station_change(self):
        monitor, log, _ = make_monitor()
        finish(monitor, monitor.next_comm(), value=1)
        monitor.accept_line("write 100")
        monitor.accept_line("station 2")
        finish(monitor, monitor.next_comm())
        comm = monitor.next_comm()
        finish(monitor, comm, ok=False)  # 재조회 실패
        monitor._next_read = 0
        comm = monitor.next_comm()
        self.assertEqual(comm.station, 2)
        finish(monitor, comm, value=9)
        self.assertNotIn("expected", of_type(log, "observation")[-1])

    def test_rejected_line_not_queued(self):
        monitor, log, _ = make_monitor()
        self.assertFalse(monitor.accept_line("write 70000"))
        self.assertEqual(len(of_type(log, "request_rejected")), 1)
        self.assertEqual(monitor.queue_length, 0)

    def test_quit_line(self):
        monitor, log, _ = make_monitor()
        self.assertTrue(monitor.accept_line("quit"))
        self.assertFalse(monitor.accept_line("status"))

    def test_status_while_waiting(self):
        monitor, log, clock = make_monitor()
        finish(monitor, monitor.next_comm(), ok=False)
        clock.now += 1.0
        monitor.next_comm()
        clock.now += 0.4
        monitor.show_status()
        status = of_type(log, "status_shown")[0]
        self.assertIn("실패", status["last_read"])
        self.assertIn("응답 대기 0.4초", status["waiting"])
        finish(monitor, monitor.in_flight, value=5)
        monitor.show_status()
        status = of_type(log, "status_shown")[1]
        self.assertEqual(status["last_read"], "정상")
        self.assertNotIn("waiting", status)

    def test_heartbeat_schedule(self):
        monitor, log, clock = make_monitor()
        self.assertTrue(monitor.heartbeat_due())
        monitor.heartbeat()
        self.assertFalse(monitor.heartbeat_due())
        clock.now += 0.5
        self.assertTrue(monitor.heartbeat_due())
        monitor.next_comm()
        clock.now += 0.3
        monitor.heartbeat()
        self.assertEqual(of_type(log, "heartbeat")[-1]["waiting"], "read")
        self.assertAlmostEqual(of_type(log, "heartbeat")[-1]["waited"], 0.3, places=3)


class ReplyTests(unittest.TestCase):
    def test_read_value(self):
        self.assertEqual(plc_common.read_value(FakeReply([65535])), 65535)

    def test_read_value_rejects(self):
        for reply in (FakeReply([1], error=True), FakeReply([]), FakeReply([1, 2]), FakeReply([1], function_code=6)):
            with self.subTest(reply=reply.registers), self.assertRaises(ValueError):
                plc_common.read_value(reply)

    def test_write_echo_mismatch(self):
        plc_common.check_write(FakeReply([100], function_code=6), 100)
        for reply in (FakeReply([99], function_code=6), FakeReply([100], error=True, function_code=6), FakeReply([], function_code=6), FakeReply([100], function_code=3)):
            with self.subTest(reply=reply.registers), self.assertRaises(ValueError):
                plc_common.check_write(reply, 100)


class LogTests(unittest.TestCase):
    def test_log_lines(self):
        clock = FakeClock()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.jsonl"
            stream = io.StringIO()
            log = plc_common.EventLog(path, stream=stream, clock=clock)
            clock.now += 1.5
            log.record("observation", value=3)
            log.close()
            event = json.loads(path.read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual((event["type"], event["value"], event["elapsed"]), ("observation", 3, 1.5))
        self.assertIn("time", event)
        self.assertIn("관측 value=3", stream.getvalue())

    def test_log_path_named_by_mode(self):
        self.assertIn("plc-sync-", plc_common.new_log_path("sync").name)


if __name__ == "__main__":
    unittest.main()
