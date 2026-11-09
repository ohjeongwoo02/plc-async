"""과제 1. asyncio 비동기판.

입력, 표시, 통신을 코루틴으로 나눈다. 통신 응답을 기다리는 동안에도 입력과 표시가 진행된다.
통신은 코루틴 하나만 하므로 같은 연결의 요청은 한 번에 하나씩 처리된다.
"""
import asyncio
import contextlib

from pymodbus.client import AsyncModbusSerialClient

from plc_common import (
    COMM_ERRORS, REGISTER_ADDRESS, KeyLines, Monitor, capture_library_log, check_write, error_text,
    parse_args, read_value, record_open_failure, start_log,
)

INPUT_INTERVAL = 0.05
DISPLAY_CHECK = 0.05
IDLE_SLEEP = 0.02


async def perform(client, comm, monitor):
    try:
        # 연결이 없으면 await 전 호출 단계에서 예외가 나므로 호출식까지 try 안에 둔다.
        if comm.kind == "read":
            reply = await client.read_holding_registers(REGISTER_ADDRESS, count=1, device_id=comm.station)
            value = read_value(reply)
        else:
            reply = await client.write_register(REGISTER_ADDRESS, comm.value, device_id=comm.station)
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


async def input_loop(monitor, keys):
    """종료 명령을 받으면 끝난다."""
    while True:
        for line in keys.poll():
            if monitor.accept_line(line):
                return
        await asyncio.sleep(INPUT_INTERVAL)


async def display_loop(monitor):
    while True:
        if monitor.heartbeat_due():
            monitor.heartbeat()
        await asyncio.sleep(DISPLAY_CHECK)


def poll_task_dead(client):
    """COM이 끊기면 라이브러리의 폴링 태스크만 죽고 재연결은 시작되지 않을 수 있다."""
    task = getattr(client.ctx.transport, "poll_task", None)
    return task is not None and task.done()


async def comm_loop(client, monitor, log):
    closed = False
    while True:
        comm = monitor.next_comm()
        if not comm:
            await asyncio.sleep(IDLE_SLEEP)
            continue
        await perform(client, comm, monitor)
        if closed or poll_task_dead(client):
            with contextlib.suppress(OSError):
                client.close()
            closed = not await client.connect()
            log.record("port_reopened", ok=not closed)


async def main_async(config, log, keys):
    library_log = capture_library_log()
    log.record("started", mode="async", config=config.as_dict())
    client = AsyncModbusSerialClient(config.port, **config.client_kwargs())
    if not await client.connect():
        record_open_failure(log, config, library_log)
        return 1
    monitor = Monitor(config, log)
    tasks = [
        asyncio.create_task(input_loop(monitor, keys)),
        asyncio.create_task(display_loop(monitor)),
        asyncio.create_task(comm_loop(client, monitor, log)),
    ]
    reason = "quit"
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    except asyncio.CancelledError:  # Ctrl+C. asyncio.run이 KeyboardInterrupt로 바꿔 올린다.
        reason = "ctrl_c"
        raise
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        monitor.stop(reason)
        client.close()
    return 0


def run(config, log, keys):
    return asyncio.run(main_async(config, log, keys))


def main():
    config = parse_args()
    log = start_log("async")
    try:
        return run(config, log, KeyLines(log))
    except KeyboardInterrupt:
        return 0
    finally:
        log.close()


if __name__ == "__main__":
    raise SystemExit(main())
