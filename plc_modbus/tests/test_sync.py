"""plc_project_sync.py 테스트. 실제 PyModbus 스택과 시험 서버를 거친다."""
import ast
from pathlib import Path
import socket
import struct
import unittest

import plc_common
import plc_project_sync
from tests.helpers import between, of_type, run_scenario, test_config, when
from tests.rtu_server import RtuServer, crc16
from tests.scenarios import CommonScenarios


def frame(station):
    body = struct.pack(">BBHH", station, 3, 0, 1)
    return body + crc16(body)


class ServerTests(unittest.TestCase):
    def test_server_silent_for_missing_station(self):
        with RtuServer(value=9) as server:
            host, port = server.url.removeprefix("socket://").split(":")
            with socket.create_connection((host, int(port)), timeout=1) as sock:
                sock.settimeout(0.3)
                sock.sendall(frame(2))
                with self.assertRaises(TimeoutError):
                    sock.recv(16)
                sock.sendall(frame(1))
                reply = sock.recv(16)
        self.assertEqual(reply[:5], bytes([1, 3, 2, 0, 9]))
        self.assertEqual(reply[5:], crc16(reply[:5]))


class HelperTests(unittest.TestCase):
    def test_deadline_fails_instead_of_hanging(self):
        with RtuServer() as server, self.assertRaises(AssertionError):
            run_scenario(plc_project_sync.run, test_config(server.url), [(when("never"), "quit\r")], deadline=1)


class SyncScenarios(CommonScenarios, unittest.TestCase):
    runner = staticmethod(plc_project_sync.run)

    def test_input_delayed_during_wait(self):
        with RtuServer(value=7) as server:
            _, events = self.play(test_config(server.url), [
                (when("observation"), "station 2\r"),
                (when("comm_start", station=2), "status\rwrite 300\r"),
                (when("write_result", value=300), "quit\r"),
            ])
        start = of_type(events, "comm_start", station=2)[0]
        during = between(events, start, "comm_end")
        self.assertEqual([e["type"] for e in during], [])
        end = of_type(events, "comm_end", station=2)[0]
        first_key = next(e for e in events[events.index(end):] if e["type"] == "key_seen")
        self.assertGreaterEqual(first_key["elapsed"], end["elapsed"])
        status = of_type(events, "status_shown")[0]
        accepted = of_type(events, "request_accepted", value=300)[0]
        self.assertLess(events.index(status), events.index(accepted))
        self.assertGreater(status["elapsed"], end["elapsed"])

    def test_quit_after_return(self):
        with RtuServer() as server:
            _, events = self.play(test_config(server.url, station=2), [
                (when("comm_start"), "quit\r"),
            ])
        stopped = of_type(events, "stopped")[0]
        self.assertIsNone(stopped["interrupted"])
        self.assertGreater(stopped["elapsed"], of_type(events, "comm_end")[0]["elapsed"])
        self.assertGreaterEqual(of_type(events, "comm_end")[0]["duration"], 0.45)

    def test_no_concurrency_imports(self):
        banned = {"asyncio", "threading", "multiprocessing", "subprocess", "concurrent"}
        for module in (plc_project_sync, plc_common):
            imported = set()
            for node in ast.walk(ast.parse(Path(module.__file__).read_text(encoding="utf-8"))):
                if isinstance(node, ast.Import):
                    imported |= {alias.name.split(".")[0] for alias in node.names}
                elif isinstance(node, ast.ImportFrom):
                    imported.add((node.module or "").split(".")[0])
            self.assertFalse(imported & banned, module.__name__)


if __name__ == "__main__":
    unittest.main()
