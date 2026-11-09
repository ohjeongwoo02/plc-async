"""실행 기록을 요약한다: python tools\\summarize_log.py <로그 파일>...

0.5초 이상 걸린 통신 호출마다, 그동안 입력과 표시가 실제로 처리됐는지 보여 준다.
"""
import json
from pathlib import Path
import sys

MIN_WAIT = 0.5


def _wait_summary(events, start_index, end_index):
    start, end = events[start_index], events[end_index]
    during = events[start_index + 1:end_index]

    def count(*event_types):
        return sum(e["type"] in event_types for e in during)

    next_key = next((e for e in events[end_index + 1:] if e["type"] == "key_seen"), None)
    after = None
    if next_key and not count("key_seen"):
        after = round(next_key["elapsed"] - end["elapsed"], 3)
    return {
        "kind": start["kind"], "station": start["station"], "start": start["elapsed"],
        "duration": end["duration"], "keys_during": count("key_seen"),
        "accepted_during": count("request_accepted", "request_rejected"),
        "status_during": count("status_shown"), "heartbeats_during": count("heartbeat"),
        "first_key_after_return": after,
    }


def summarize(events):
    beats = [e["elapsed"] for e in events if e["type"] == "heartbeat"]
    waits = []
    start_index = None
    for index, item in enumerate(events):
        if item["type"] == "comm_start":
            start_index = index
        elif item["type"] == "comm_end" and item["duration"] >= MIN_WAIT:
            waits.append(_wait_summary(events, start_index, index))
    return {
        "mode": next((e.get("mode") for e in events if e["type"] == "started"), None),
        "observations": sum(e["type"] == "observation" for e in events),
        "read_failures": sum(e["type"] == "read_failed" for e in events),
        "heartbeat_max_gap": round(max((b - a for a, b in zip(beats, beats[1:])), default=0), 3),
        "waits": waits,
    }


def main(paths):
    for path in paths:
        text = Path(path).read_text(encoding="utf-8")
        summary = summarize([json.loads(line) for line in text.splitlines()])
        print(f"== {path}")
        print(f"판: {summary['mode']}  관측: {summary['observations']}  읽기 실패: {summary['read_failures']}"
              f"  표시 최대 간격: {summary['heartbeat_max_gap']}초")
        for wait in summary["waits"]:
            print(f"  {wait['start']:8.3f}s {wait['kind']} 국번{wait['station']} {wait['duration']}초 대기 | "
                  f"대기 중 키 {wait['keys_during']}, 접수 {wait['accepted_during']}, "
                  f"status {wait['status_during']}, 표시 {wait['heartbeats_during']} | "
                  f"반환 뒤 첫 키까지 {wait['first_key_after_return']}초")


if __name__ == "__main__":
    main(sys.argv[1:])
