"""plc_project.py 테스트. 실제 PyModbus 스택과 시험 서버를 거친다."""
import unittest
from unittest import mock

import plc_project
from tests.helpers import between, of_type, test_config, when
from tests.rtu_server import RtuServer
from tests.scenarios import CommonScenarios


class AsyncScenarios(CommonScenarios, unittest.TestCase):
    runner = staticmethod(plc_project.run)

    def test_input_during_wait(self):
        with RtuServer(value=7) as server:
            _, events = self.play(test_config(server.url, timeout=1.0), [
                (when("observation"), "station 2\r"),
                (when("comm_start", station=2), "status\rwrite 300\r"),
                (when("write_result", value=300), "quit\r"),
            ])
        start = of_type(events, "comm_start", station=2)[0]
        during = between(events, start, "comm_end")
        types = [e["type"] for e in during]
        self.assertIn("key_seen", types)
        self.assertIn("status_shown", types)
        self.assertIn("request_accepted", types)
        status = of_type(during, "status_shown")[0]
        self.assertIn("응답 대기", status["waiting"])
        self.assertEqual(status["last_read"], "정상")
        self.assertLess(status["elapsed"] - start["elapsed"], 0.3)

    def test_heartbeat_during_wait(self):
        with RtuServer() as server:
            _, events = self.play(test_config(server.url, station=2, timeout=1.6), [
                (when("read_failed"), "quit\r"),
            ])
        start = of_type(events, "comm_start")[0]
        beats = of_type(between(events, start, "comm_end"), "heartbeat")
        self.assertGreaterEqual(len(beats), 2)
        self.assertEqual(beats[0]["waiting"], "read")
        times = [start["elapsed"]] + [b["elapsed"] for b in beats]
        self.assertLess(max(b - a for a, b in zip(times, times[1:])), 1.0)

    def test_one_request_at_a_time(self):
        with RtuServer(value=7) as server:
            _, events = self.play(test_config(server.url), [
                (when("observation"), "write 1\rwrite 2\rstation 2\rwrite 3\rstation 1\rwrite 4\r"),
                (when("write_result", value=4), "quit\r"),
            ])
            self.assertEqual(server.writes(station=1), [1, 2, 4])
            self.assertEqual(server.writes(station=2), [3])
        kinds = [e["type"] for e in events if e["type"] in ("comm_start", "comm_end")]
        for first, second in zip(kinds, kinds[1:]):
            self.assertNotEqual(first, second)

    def test_quit_during_wait(self):
        with RtuServer() as server:
            for _ in range(2):  # 두 번째 실행은 포트가 닫혔는지 확인한다.
                code, events = self.play(test_config(server.url, station=2, timeout=3.0), [
                    (when("comm_start"), "quit\r"),
                ])
                self.assertEqual(code, 0)
                stopped = of_type(events, "stopped")[0]
                self.assertEqual(stopped["interrupted"], "read")
                self.assertLess(stopped["elapsed"], 1.0)
                self.assertEqual(of_type(events, "comm_end"), [])

    def test_reopens_after_poll_task_dies(self):
        """COM 끊김 때 라이브러리 폴링 태스크만 죽고 재연결이 안 시작되는 경우를 복구한다."""
        clients = []

        class Tracking(plc_project.AsyncModbusSerialClient):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                clients.append(self)

        def kill_poll():
            clients[0].ctx.transport.poll_task.cancel()

        with RtuServer(value=7) as server, mock.patch.object(plc_project, "AsyncModbusSerialClient", Tracking):
            _, events = self.play(test_config(server.url), [
                (when("observation"), kill_poll),
                (when("read_failed"), lambda: None),
                (when("observation"), "quit\r"),
            ])
        self.assertEqual([e["ok"] for e in of_type(events, "port_reopened")], [True])

    def test_observation_only_server_value(self):
        with RtuServer(value=7) as server:
            _, events = self.play(test_config(server.url), [
                (when("observation"), "station 2\rwrite 1\rwrite 2\rstation 1\r"),
                (when("station_changed", station=1), lambda: None),
                (when("observation"), "quit\r"),
            ])
        self.assertEqual({e["value"] for e in of_type(events, "observation")}, {7})


if __name__ == "__main__":
    unittest.main()
