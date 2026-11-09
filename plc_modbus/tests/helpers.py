"""시나리오 테스트 도구. 기록된 사건을 조건으로 키 입력을 흉내 낸다."""
import io
from pathlib import Path
import tempfile
import time

import plc_common


def test_config(url, **over):
    fields = {**plc_common.Config.defaults(), "port": url, "timeout": 0.5, "poll_interval": 0.2}
    return plc_common.Config(**{**fields, **over})


def of_type(events, event_type, /, **fields):
    return [e for e in events if e["type"] == event_type and all(e.get(k) == v for k, v in fields.items())]


def when(event_type, /, nth=1, **fields):
    """앞 단계 뒤에 이 사건이 nth번 기록되면 참이다."""
    def check(new_events):
        return len(of_type(new_events, event_type, **fields)) >= nth
    return check


class ScriptedKeys:
    """msvcrt 대신 쓰는 키 입력. 단계마다 (조건, 입력 문자열 또는 함수)다.

    프로그램이 kbhit을 부르는 시점에만 조건을 확인한다. 실제 키보드처럼 키는 쌓여 있다가
    프로그램이 읽을 때 전달된다.
    """

    def __init__(self, steps, deadline):
        self._steps = list(steps)
        self._chars = []
        self._seen = 0
        self._deadline = time.monotonic() + deadline
        self.events = None
        self.deadline_hit = False

    def kbhit(self):
        if not self._chars:
            self._advance()
        return bool(self._chars)

    def getwch(self):
        return self._chars.pop(0)

    def _advance(self):
        while self._steps and self._steps[0][0](self.events[self._seen:]):
            _, action = self._steps.pop(0)
            self._seen = len(self.events)
            if callable(action):
                action()
            else:
                self._chars.extend(action)
                return
        if not self.deadline_hit and time.monotonic() > self._deadline:
            self.deadline_hit = True
            self._chars.append("\x03")


def run_scenario(runner, config, steps, deadline=15):
    """runner(config, log, keys)를 실행하고 (종료 코드, 사건 목록)을 돌려준다."""
    with tempfile.TemporaryDirectory() as tmp:
        log = plc_common.EventLog(Path(tmp) / "run.jsonl", stream=io.StringIO(), keep_events=True)
        source = ScriptedKeys(steps, deadline)
        source.events = log.events
        try:
            code = runner(config, log, plc_common.KeyLines(log, source))
        finally:
            log.close()
    if source.deadline_hit:
        raise AssertionError(f"시나리오가 제한 시간 안에 끝나지 않음. 남은 단계 {len(source._steps)}")
    return code, log.events


def between(events, start, end_kind):
    """start 사건부터 다음 end_kind 사건까지의 사건 목록을 돌려준다."""
    index = events.index(start)
    for stop in range(index + 1, len(events)):
        if events[stop]["type"] == end_kind:
            return events[index + 1:stop]
    raise AssertionError(f"{end_kind} 없음")
