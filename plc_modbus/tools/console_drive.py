"""PLC 없이 전체 프로그램을 실제 콘솔 키 입력으로 실행하는 검증 도구.

숨겨진 새 콘솔에서 프로그램을 띄우고 WriteConsoleInput으로 진짜 키 이벤트를 넣는다.
그래서 msvcrt 경로까지 거치며, 키를 누른 시각을 프로그램 밖에서 잴 수 있다.
장치는 tests/rtu_server.py의 시험 서버이므로 실제 PLC 결과가 아니다.

    python tools\\console_drive.py {async|sync}
    python tools\\console_drive.py {async|sync} --ctrl-c
"""
import argparse
import ctypes
from ctypes import wintypes
from datetime import datetime
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.rtu_server import RtuServer  # noqa: E402

PROGRAMS = {"async": "plc_project.py", "sync": "plc_project_sync.py"}
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
KEY_EVENT = 1
STD_INPUT_HANDLE = -10
CTRL_C_EVENT = 0


class _KeyEvent(ctypes.Structure):
    _fields_ = [("bKeyDown", wintypes.BOOL), ("wRepeatCount", wintypes.WORD),
                ("wVirtualKeyCode", wintypes.WORD), ("wVirtualScanCode", wintypes.WORD),
                ("UnicodeChar", wintypes.WCHAR), ("dwControlKeyState", wintypes.DWORD)]


class _InputRecord(ctypes.Structure):
    class _Event(ctypes.Union):
        _fields_ = [("KeyEvent", _KeyEvent), ("padding", ctypes.c_byte * 16)]
    _fields_ = [("EventType", wintypes.WORD), ("Event", _Event)]


def now():
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


class Console:
    """자식 프로그램의 콘솔에 붙어 키를 넣는다."""

    def __init__(self, pid):
        kernel32.FreeConsole()
        for _ in range(100):
            if kernel32.AttachConsole(pid):
                break
            time.sleep(0.05)
        else:
            raise OSError(ctypes.get_last_error(), "콘솔 연결 실패")
        kernel32.GetStdHandle.restype = wintypes.HANDLE
        kernel32.CreateFileW.restype = wintypes.HANDLE
        self._input = kernel32.CreateFileW("CONIN$", 0xC0000000, 3, None, 3, 0, None)
        kernel32.SetConsoleCtrlHandler(None, True)  # 드라이버는 Ctrl+C로 죽지 않게 한다.

    def press(self, text):
        records = (_InputRecord * (len(text) * 2))()
        for index, char in enumerate(text):
            for offset, down in enumerate((True, False)):
                record = records[index * 2 + offset]
                record.EventType = KEY_EVENT
                record.Event.KeyEvent.bKeyDown = down
                record.Event.KeyEvent.wRepeatCount = 1
                record.Event.KeyEvent.UnicodeChar = char
        written = wintypes.DWORD()
        if not kernel32.WriteConsoleInputW(wintypes.HANDLE(self._input), records, len(records), ctypes.byref(written)):
            raise ctypes.WinError(ctypes.get_last_error())

    def ctrl_c(self):
        kernel32.GenerateConsoleCtrlEvent(CTRL_C_EVENT, 0)


class LogWatch:
    def __init__(self, logs_dir, known):
        self._dir, self._known = logs_dir, known
        self._path, self._seen = None, 0
        self.events = []

    def wait(self, event_type, timeout=15.0, **fields):
        """이전 wait 뒤에 새로 기록된 사건 중 조건에 맞는 것을 기다린다."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._refresh()
            while self._seen < len(self.events):
                event = self.events[self._seen]
                self._seen += 1
                if event["type"] == event_type and all(event.get(k) == v for k, v in fields.items()):
                    return event
            time.sleep(0.01)
        raise TimeoutError(f"{event_type} {fields} 기록 없음")

    def _refresh(self):
        if self._path is None:
            new = sorted(set(self._dir.glob("plc-*.jsonl")) - self._known)
            if not new:
                return
            self._path = new[-1]
        lines = self._path.read_text(encoding="utf-8").splitlines()
        self.events = [json.loads(line) for line in lines]

    @property
    def path(self):
        return self._path


def scenario(console, watch, presses):
    def press(label, text):
        console.press(text)
        presses.append({"label": label, "text": text.strip(), "pressed_at": now()})

    watch.wait("observation")
    press("시험1 write 100", "write 100\r")
    watch.wait("observation", expected=100)
    press("시험1 write 200", "write 200\r")
    watch.wait("observation", expected=200)
    press("시험2 station 2", "station 2\r")
    watch.wait("comm_start", station=2, kind="read")
    press("시험2 대기 중 status", "status\r")
    watch.wait("status_shown")
    watch.wait("comm_start", station=2, kind="read")
    press("시험2 대기 중 write 300", "write 300\r")
    watch.wait("write_result", value=300)
    press("시험3 station 1", "station 1\r")
    watch.wait("station_changed", station=1)
    watch.wait("observation", station=1)
    press("시험3 write 400", "write 400\r")
    watch.wait("observation", expected=400)
    watch.wait("heartbeat")
    press("종료", "quit\r")


def ctrl_c_scenario(console, watch, presses):
    watch.wait("observation")
    console.press("station 2\r")
    presses.append({"label": "station 2", "text": "station 2", "pressed_at": now()})
    watch.wait("comm_start", station=2, kind="read")
    time.sleep(0.2)  # 무응답 대기 중에 누른다.
    console.ctrl_c()
    presses.append({"label": "대기 중 Ctrl+C", "text": "", "pressed_at": now()})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=PROGRAMS)
    parser.add_argument("--ctrl-c", action="store_true")
    args = parser.parse_args()
    logs = ROOT / "logs"
    logs.mkdir(exist_ok=True)
    known = set(logs.glob("plc-*.jsonl"))
    with RtuServer(stations=(1,), value=7) as server, tempfile.TemporaryDirectory() as tmp:
        config = json.loads((ROOT / "plc_config.json").read_text(encoding="utf-8"))
        config["port"] = server.url  # 나머지 통신 설정은 실제 설정 파일 값 그대로다.
        path = Path(tmp) / "config.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        info = subprocess.STARTUPINFO()
        info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        info.wShowWindow = subprocess.SW_HIDE
        child = subprocess.Popen(
            [sys.executable, str(ROOT / PROGRAMS[args.mode]), "--config", str(path)],
            cwd=ROOT, creationflags=subprocess.CREATE_NEW_CONSOLE, startupinfo=info,
        )
        presses = []
        watch = LogWatch(logs, known)
        try:
            console = Console(child.pid)
            (ctrl_c_scenario if args.ctrl_c else scenario)(console, watch, presses)
            code = child.wait(timeout=15)
        finally:
            if child.poll() is None:
                child.kill()
        watch.wait("stopped", timeout=2)
    record = watch.path.with_suffix(".presses.json")
    record.write_text(json.dumps(presses, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"exit={code} log={watch.path} stopped={watch.events[-1]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
