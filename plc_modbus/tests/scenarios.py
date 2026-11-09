"""두 판이 같은 입력·같은 설정으로 통과해야 하는 시나리오."""
from tests.helpers import of_type, run_scenario, test_config, when
from tests.rtu_server import RtuServer

BAD_LINES = ["", "write 65536", "write -1", "write 1.5", "station 0", "station 3", "쓰기 100", "write １００"]


class CommonScenarios:
    """상속한 클래스가 runner(config, log, keys)를 정의한다."""

    runner = None

    def play(self, config, steps):
        return run_scenario(type(self).runner, config, steps)

    def test_write_then_read(self):
        with RtuServer(value=7) as server:
            code, events = self.play(test_config(server.url), [
                (when("observation"), "write 100\r"),
                (when("observation", expected=100), "write 200\r"),
                (when("observation", expected=200), "quit\r"),
            ])
            self.assertEqual(server.value, 200)
        self.assertEqual(code, 0)
        self.assertEqual(of_type(events, "observation")[0]["value"], 7)
        results = of_type(events, "write_result")
        self.assertEqual([(r["value"], r["ok"]) for r in results], [(100, True), (200, True)])
        for value in (100, 200):
            accepted = events.index(of_type(events, "request_accepted", value=value)[0])
            result = events.index(of_type(events, "write_result", value=value)[0])
            seen = of_type(events, "observation", expected=value)[0]
            self.assertLess(accepted, result)
            self.assertLess(result, events.index(seen))
            self.assertEqual((seen["value"], seen["matches"]), (value, True))

    def test_no_response_read_failed(self):
        with RtuServer(value=7) as server:
            _, events = self.play(test_config(server.url), [
                (when("observation"), "station 2\r"),
                (when("read_failed"), "status\r"),
                (when("status_shown"), "quit\r"),
            ])
        ended = of_type(events, "comm_end", station=2, kind="read")[0]
        self.assertGreaterEqual(ended["duration"], 0.45)
        self.assertLess(ended["duration"], 1.0)
        self.assertEqual(of_type(events, "observation", station=2), [])
        self.assertNotIn("value", of_type(events, "read_failed")[0])
        status = of_type(events, "status_shown")[-1]
        self.assertEqual((status["observed_value"], status["observed_station"]), (7, 1))
        self.assertIn("실패", status["last_read"])

    def test_start_station2_unobserved(self):
        with RtuServer() as server:
            _, events = self.play(test_config(server.url, station=2), [
                (when("read_failed"), "status\r"),
                (when("status_shown"), "quit\r"),
            ])
        self.assertEqual(of_type(events, "observation"), [])
        status = of_type(events, "status_shown")[0]
        self.assertEqual(status["observed"], "미관측")
        self.assertNotIn("observed_value", status)

    def test_restore_station1(self):
        with RtuServer(value=7) as server:
            _, events = self.play(test_config(server.url), [
                (when("observation"), "station 2\r"),
                (when("comm_start", station=2), "write 555\r"),
                (when("write_result", value=555), "station 1\r"),
                (when("observation", station=1), "write 300\r"),
                (when("observation", expected=300), "quit\r"),
            ])
            self.assertEqual(server.writes(station=2), [555])
            self.assertEqual(server.writes(station=1), [300])
            self.assertEqual(server.value, 300)
        self.assertIs(of_type(events, "write_result", value=555)[0]["ok"], False)
        restored = events.index(of_type(events, "station_changed", station=1)[0])
        after = [e for e in events[restored:] if e["type"] == "observation"]
        self.assertEqual(after[0]["value"], 7)
        self.assertEqual((after[-1]["value"], after[-1]["matches"]), (300, True))
        before = of_type(events, "observation")[0]
        self.assertGreater(after[0]["elapsed"], before["elapsed"])

    def test_server_drop_survives(self):
        with RtuServer(value=7) as server:
            _, events = self.play(test_config(server.url), [
                (when("observation"), server.drop_clients),
                (when("observation"), "quit\r"),
            ])
        observed = of_type(events, "observation")
        self.assertGreaterEqual(len(observed), 2)
        self.assertEqual(of_type(events, "stopped")[0]["reason"], "quit")

    def test_late_write_echo_not_observation(self):
        """timeout 뒤 늦게 온 쓰기 응답이 다음 읽기의 관측값으로 둔갑하면 안 된다."""
        with RtuServer(value=7, late_write_delay=0.6) as server:
            _, events = self.play(test_config(server.url), [
                (when("observation"), "write 100\r"),
                (when("write_result", value=100), lambda: None),
                (when("observation"), "quit\r"),
            ])
            self.assertEqual(server.value, 7)
        self.assertIs(of_type(events, "write_result", value=100)[0]["ok"], False)
        self.assertEqual({e["value"] for e in of_type(events, "observation")}, {7})

    def test_bad_input_sends_nothing(self):
        with RtuServer() as server:
            text = "".join(f"{line}\r" for line in BAD_LINES)
            _, events = self.play(test_config(server.url), [
                (when("observation"), text),
                (when("request_rejected", nth=len(BAD_LINES)), "quit\r"),
            ])
            self.assertEqual(server.writes(), [])
        self.assertEqual({r[0] for r in server.requests}, {1})
        self.assertEqual(len(of_type(events, "request_rejected")), len(BAD_LINES))

    def test_open_failure_exit_1(self):
        code, events = self.play(test_config("COM99"), [])
        self.assertEqual(code, 1)
        failed = of_type(events, "open_failed")[0]
        self.assertIn("COM99", failed["reason"])
        self.assertNotIn("알리지 않음", failed["reason"])
        self.assertEqual(of_type(events, "comm_start"), [])

    def test_can_reopen_after_quit(self):
        with RtuServer(value=7) as server:
            for _ in range(2):
                code, events = self.play(test_config(server.url), [(when("observation"), "quit\r")])
                self.assertEqual(code, 0)
                self.assertEqual(len(of_type(events, "stopped")), 1)
