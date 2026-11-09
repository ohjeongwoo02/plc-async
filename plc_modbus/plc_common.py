"""두 프로그램이 함께 쓰는 설정, 명령 해석, 기록, 키 입력, 판단 로직.

통신과 실행 흐름은 넣지 않는다. 그 부분만 plc_project.py와 plc_project_sync.py에서 다르다.
"""
import argparse
from collections import deque
from dataclasses import dataclass, replace
from datetime import datetime
import json
import logging
import msvcrt
from pathlib import Path
import re
import sys
import time

from pymodbus.exceptions import ModbusException

ROOT = Path(__file__).parent
REGISTER_ADDRESS = 0  # D01000. 코드에 고정하며 명령으로 바꿀 수 없다.
MAX_VALUE = 65535
ALLOWED_STATIONS = (1, 2)
DISPLAY_INTERVAL = 0.5
COMM_ERRORS = (ModbusException, OSError, ValueError)

LABELS = {
    "started": "시작",
    "open_failed": "포트 열기 실패",
    "request_accepted": "요청 접수",
    "request_rejected": "요청 거절",
    "status_shown": "상태",
    "comm_start": "통신 시작",
    "comm_end": "통신 반환",
    "write_result": "쓰기 결과",
    "station_changed": "국번 변경",
    "port_reopened": "포트 다시 열기",
    "observation": "관측",
    "read_failed": "읽기 실패",
    "heartbeat": "응답 대기 중",
    "stopped": "종료",
}


@dataclass(frozen=True)
class Config:
    port: str
    baudrate: int
    bytesize: int
    parity: str
    stopbits: int
    station: int
    timeout: float
    retries: int
    poll_interval: float

    def __post_init__(self):
        if self.station not in ALLOWED_STATIONS:
            raise ValueError(f"국번은 {ALLOWED_STATIONS} 중 하나여야 합니다: {self.station}")
        if self.timeout <= 0 or self.poll_interval <= 0 or self.retries < 0:
            raise ValueError("timeout과 poll_interval은 0보다 크고 retries는 0 이상이어야 합니다")

    @staticmethod
    def defaults():
        return {
            "port": "COM5", "baudrate": 9600, "bytesize": 8, "parity": "N", "stopbits": 1,
            "station": 1, "timeout": 2.0, "retries": 0, "poll_interval": 1.0,
        }

    def client_kwargs(self):
        return {
            "baudrate": self.baudrate, "bytesize": self.bytesize, "parity": self.parity,
            "stopbits": self.stopbits, "timeout": self.timeout, "retries": self.retries,
        }

    def as_dict(self):
        return {**self.client_kwargs(), "port": self.port, "station": self.station,
                "poll_interval": self.poll_interval}


def load_config(path):
    return Config(**json.loads(Path(path).read_text(encoding="utf-8")))


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="D01000 감시와 값 변경")
    parser.add_argument("--config", default=ROOT / "plc_config.json", help="설정 파일 경로")
    parser.add_argument("--station", type=int, choices=ALLOWED_STATIONS, help="요청 대상 국번")
    args = parser.parse_args(argv)
    config = load_config(args.config)
    return replace(config, station=args.station) if args.station else config


@dataclass(frozen=True)
class Command:
    kind: str
    value: int | None = None


def _number(text):
    # int()는 전각 숫자와 "1_000"도 받으므로 형식을 먼저 좁힌다.
    if not re.fullmatch(r"[0-9]+", text):
        raise ValueError(f"정수가 아닙니다: {text}")
    return int(text)


def parse_command(line):
    parts = line.split()
    name = parts[0].lower() if parts else ""
    if name in ("status", "quit") and len(parts) == 1:
        return Command(name)
    if name in ("write", "station") and len(parts) == 2:
        value = _number(parts[1])
        if name == "write" and value > MAX_VALUE:
            raise ValueError(f"값은 0~{MAX_VALUE} 범위여야 합니다: {value}")
        if name == "station" and value not in ALLOWED_STATIONS:
            raise ValueError(f"국번은 {ALLOWED_STATIONS} 중 하나여야 합니다: {value}")
        return Command(name, value)
    raise ValueError("명령 형식: write <0~65535> | station <1|2> | status | quit")


class EventLog:
    """실행 기록을 JSONL 파일과 터미널에 함께 쓴다."""

    def __init__(self, path, stream=None, clock=time.monotonic, keep_events=False):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.path.open("a", encoding="utf-8")
        self._stream = sys.stdout if stream is None else stream
        self._clock = clock
        self._start = clock()
        self._prompt = ""
        self.events = [] if keep_events else None

    def record(self, event_type, /, show=True, **fields):
        now = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        event = {"type": event_type, "elapsed": round(self._clock() - self._start, 3), "time": now, **fields}
        self._file.write(json.dumps(event, ensure_ascii=False) + "\n")
        self._file.flush()
        if self.events is not None:
            self.events.append(event)
        if show:
            self._show(event)
        return event

    def _show(self, event):
        details = " ".join(f"{k}={v}" for k, v in event.items() if k not in ("type", "elapsed", "time"))
        label = LABELS.get(event["type"], event["type"])
        line = f"[{event['elapsed']:8.3f}s {event['time']}] {label} {details}".rstrip()
        self._stream.write(f"\r{' ' * len(self._prompt)}\r{line}\n{self._prompt}")
        self._stream.flush()

    def set_prompt(self, text):
        """입력 중인 글자를 화면에 찍는다. 프로그램이 키를 읽었을 때만 호출된다."""
        padding = " " * max(0, len(self._prompt) - len(text))
        self._stream.write(f"\r{text}{padding}\r{text}")
        self._stream.flush()
        self._prompt = text

    def end_prompt(self):
        self._stream.write("\n")
        self._stream.flush()
        self._prompt = ""

    def close(self):
        self._file.close()


def new_log_path(mode):
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    return ROOT / "logs" / f"plc-{mode}-{stamp}.jsonl"


def start_log(mode):
    """실행 기록을 열고 터미널 첫 줄에 기록 파일 경로와 명령을 알린다."""
    sys.stdout.reconfigure(errors="replace")
    log = EventLog(new_log_path(mode))
    print(f"기록 파일: {log.path}")
    print("명령: write <0~65535> | station <1|2> | status | quit")
    return log


class KeyLines:
    """콘솔 키를 차단 없이 읽어 완성된 줄로 돌려준다."""

    def __init__(self, log, source=msvcrt):
        self._log = log
        self._source = source
        self._buffer = ""
        self._last_key = ""

    def poll(self):
        lines = []
        while self._source.kbhit():
            key = self._source.getwch()
            if key in ("\x00", "\xe0"):  # 방향키 등 특수키는 두 글자로 들어온다.
                self._source.getwch()
                continue
            self._log.record("key_seen", show=False, key=key)
            line = self._apply(key)
            if line is not None:
                lines.append(line)
            self._last_key = key
        return lines

    def _apply(self, key):
        if key == "\x03":
            return self._finish("quit")
        if key == "\n" and self._last_key == "\r":
            return None
        if key in ("\r", "\n"):
            return self._finish(self._buffer)
        if key == "\x08":
            self._buffer = self._buffer[:-1]
        elif key.isprintable():
            self._buffer += key
        self._log.set_prompt(f"> {self._buffer}")
        return None

    def _finish(self, line):
        self._log.set_prompt(f"> {self._buffer}")
        self._log.end_prompt()
        self._buffer = ""
        return line


@dataclass(frozen=True)
class Comm:
    kind: str
    station: int
    value: int | None = None
    request_id: int | None = None


class Monitor:
    """요청 대기열, 통신 차례, 관측 상태를 판단한다. 입출력은 하지 않는다."""

    def __init__(self, config, log, clock=time.monotonic):
        self.station = config.station
        self.in_flight = None
        self.last_observation = None
        self._interval = config.poll_interval
        self._log = log
        self._clock = clock
        self._queue = deque()
        self._next_id = 1
        self._next_read = clock()
        self._next_heartbeat = clock()
        self._started_at = 0.0
        self._after_request = False
        self._read_back = False
        self._expected = None
        self._last_read = "아직 없음"

    @property
    def queue_length(self):
        return len(self._queue)

    def accept_line(self, line):
        """한 줄을 처리한다. 종료 명령이면 참을 돌려준다."""
        try:
            command = parse_command(line)
        except ValueError as error:
            self._log.record("request_rejected", line=line, reason=str(error))
            return False
        if command.kind == "status":
            self.show_status()
            return False
        if command.kind == "quit":
            self._log.record("request_accepted", command="quit")
            return True
        request_id = self._next_id
        self._next_id += 1
        self._queue.append((request_id, command))
        self._log.record("request_accepted", request_id=request_id, command=command.kind,
                         value=command.value, queue=len(self._queue))
        return False

    def next_comm(self):
        """다음 통신을 정한다. 지금 할 통신이 없으면 None이다."""
        if self._after_request:
            self._after_request = False
            if self._read_back or self._read_due():
                return self._start_read()
        while self._queue:
            request_id, command = self._queue.popleft()
            if command.kind == "station":
                self.station = command.value
                self._expected = None
                self._log.record("station_changed", request_id=request_id, station=self.station)
                continue
            return self._start(Comm("write", self.station, command.value, request_id))
        return self._start_read() if self._read_due() else None

    def _read_due(self):
        return self._clock() >= self._next_read

    def _start_read(self):
        self._read_back = False
        self._next_read = self._clock() + self._interval
        return self._start(Comm("read", self.station))

    def _start(self, comm):
        self.in_flight = comm
        self._started_at = self._clock()
        self._log.record("comm_start", kind=comm.kind, station=comm.station,
                         request_id=comm.request_id, show=comm.kind == "write" or comm.station != 1)
        return comm

    def _end(self, comm):
        duration = round(self._clock() - self._started_at, 3)
        self.in_flight = None
        self._log.record("comm_end", kind=comm.kind, station=comm.station, request_id=comm.request_id,
                         duration=duration, show=comm.kind == "write" or comm.station != 1)

    def finish_read(self, comm, value=None, error=None):
        self._end(comm)
        if value is None:
            reason = error or "응답 값 없음"
            self._last_read = f"실패 ({reason})"
            self._log.record("read_failed", station=comm.station, reason=reason)
            return
        fields = {}
        if self._expected is not None:
            fields = {"expected": self._expected, "matches": value == self._expected}
            self._expected = None
        event = self._log.record("observation", value=value, station=comm.station, **fields)
        self._last_read = "정상"
        self.last_observation = {"value": value, "station": comm.station, "time": event["time"]}

    def finish_write(self, comm, ok, error=None):
        self._end(comm)
        fields = {} if ok else {"reason": error or "알 수 없음"}
        self._log.record("write_result", request_id=comm.request_id, station=comm.station,
                         value=comm.value, ok=ok, **fields)
        self._after_request = True
        self._read_back = ok
        if ok:
            self._expected = comm.value

    def heartbeat_due(self):
        return self._clock() >= self._next_heartbeat

    def heartbeat(self):
        self._next_heartbeat = self._clock() + DISPLAY_INTERVAL
        waiting = self.in_flight.kind if self.in_flight else None
        fields = {"waiting": waiting}
        if waiting:
            fields.update(station=self.in_flight.station, waited=round(self._clock() - self._started_at, 3))
        self._log.record("heartbeat", show=bool(waiting), **fields)

    def show_status(self):
        waiting = {}
        if self.in_flight:
            waited = self._clock() - self._started_at
            waiting = {"waiting": f"응답 대기 {waited:.1f}초 (국번 {self.in_flight.station})"}
        if self.last_observation:
            observed = {
                "observed_value": self.last_observation["value"],
                "observed_at": self.last_observation["time"],
                "observed_station": self.last_observation["station"],
            }
        else:
            observed = {"observed": "미관측"}
        self._log.record("status_shown", station=self.station, queue=len(self._queue),
                         last_read=self._last_read, **waiting, **observed)

    def stop(self, reason):
        interrupted = self.in_flight.kind if self.in_flight else None
        self._log.record("stopped", reason=reason, interrupted=interrupted, queue=len(self._queue))


def read_value(reply, function_code=3):
    if reply.isError():
        raise ValueError(f"Modbus 예외 응답 (코드 {getattr(reply, 'exception_code', '?')})")
    # timeout 뒤 늦게 온 다른 요청의 응답을 이번 응답으로 받아들이지 않는다.
    if reply.function_code != function_code:
        raise ValueError(f"요청과 다른 응답입니다: FC{reply.function_code}")
    if len(reply.registers) != 1:
        raise ValueError(f"값 개수가 1이 아닙니다: {len(reply.registers)}")
    return reply.registers[0]


def check_write(reply, value):
    echoed = read_value(reply, function_code=6)
    if echoed != value:
        raise ValueError(f"쓰기 응답 값이 다릅니다: {echoed}")


def error_text(error):
    return f"{type(error).__name__}: {error}"


def record_open_failure(log, config, library_log):
    detail = library_log.text or "라이브러리가 원인을 알리지 않음"
    log.record("open_failed", reason=f"{config.port}: {detail}",
               hint="포트 번호와 다른 프로그램(QModMaster 등)의 사용 여부를 확인하세요")


class _LastMessage(logging.Handler):
    def __init__(self):
        super().__init__(logging.WARNING)  # 비동기 클라이언트는 포트 열기 실패를 WARNING으로 남긴다.
        self.text = ""

    def emit(self, record):
        self.text = record.getMessage()


def capture_library_log():
    """라이브러리 로그가 입력 줄을 어지럽히지 않게 막고, 마지막 오류를 원인 표시에 쓴다."""
    logger = logging.getLogger("pymodbus")
    handler = _LastMessage()
    logger.handlers[:] = [handler]
    logger.propagate = False
    return handler
