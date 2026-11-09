"""과제 2. 단일 실행 흐름 동기판.

한 루프에서 입력 확인, 표시, 통신을 차례로 한다. 통신 함수가 반환될 때까지 다음 일은 진행되지 않는다.
"""
import time

from pymodbus.client import ModbusSerialClient

from plc_common import (
    COMM_ERRORS, REGISTER_ADDRESS, KeyLines, Monitor, capture_library_log, check_write, error_text,
    parse_args, read_value, record_open_failure, start_log,
)

IDLE_SLEEP = 0.02


def perform(client, comm, monitor):
    """통신 한 건을 끝까지 기다린다. 응답이나 timeout 전에는 반환하지 않는다."""
    try:
        if comm.kind == "read":
            reply = client.read_holding_registers(REGISTER_ADDRESS, count=1, device_id=comm.station)
            value = read_value(reply)
        else:
            reply = client.write_register(REGISTER_ADDRESS, comm.value, device_id=comm.station)
            check_write(reply, comm.value)
    except COMM_ERRORS as error:
        if comm.kind == "read":
            monitor.finish_read(comm, error=error_text(error))
        else:
            monitor.finish_write(comm, False, error_text(error))
        return
    if comm.kind == "read":
        monitor.finish_read(comm, value=value)
    else:
        monitor.finish_write(comm, True)


def loop(client, monitor, keys):
    while True:
        for line in keys.poll():
            if monitor.accept_line(line):
                return
        if monitor.heartbeat_due():
            monitor.heartbeat()
        comm = monitor.next_comm()
        if comm:
            perform(client, comm, monitor)
        else:
            time.sleep(IDLE_SLEEP)


def run(config, log, keys):
    library_log = capture_library_log()
    log.record("started", mode="sync", config=config.as_dict())
    client = ModbusSerialClient(config.port, **config.client_kwargs())
    if not client.connect():
        record_open_failure(log, config, library_log)
        return 1
    monitor = Monitor(config, log)
    reason = "quit"
    try:
        loop(client, monitor, keys)
    except KeyboardInterrupt:
        reason = "ctrl_c"
    finally:
        monitor.stop(reason)
        client.close()
    return 0


def main():
    config = parse_args()
    log = start_log("sync")
    try:
        return run(config, log, KeyLines(log))
    finally:
        log.close()


if __name__ == "__main__":
    raise SystemExit(main())
