"""PLC <-> 로봇 배선 신호 점검 (LS Cnet 시리얼, asyncio).

- 출력 테스트: P44, P45 를 차례로 ON/OFF -> 로봇 펜던트에서 DI0/DI1 램프 확인
- 입력 모니터: P04, P05 값이 바뀌면 화면에 출력 -> 펜던트에서 DO0/DO1 수동 토글
- --seq: 래더 없이 PC 가 PLC 역할을 대신해 IO map 순서대로 진행 (D01000 에 단계 코드 기록)

사용법:
    python signal_check.py --port COM3         # 실제 PLC, 단순 점검
    python signal_check.py --port COM3 --seq   # 실제 PLC, 순서 모드
    python signal_check.py --sim [--seq]       # 가짜 PLC (로봇이 DI->DO 그대로 따라함)
    python signal_check.py --selftest          # 자체 점검
종료: Ctrl+C (P44/P45 는 OFF 로 되돌림)
"""
import argparse
import asyncio
import time

PORT = "COM3"
BAUD = 9600          # 8-N-1
STATION = 0          # Cnet 국번
PULSE_S = 1.0        # 출력 ON/OFF 유지 시간
POLL_S = 0.1         # 입력 읽기 주기
GRIP_S = 1.0         # shortcut: 그리퍼 미정이라 대기만 함, 그리퍼 정해지면 실제 제어로 교체
ROBOT_TIMEOUT_S = 30.0

# 실측: %PX44 비트쓰기는 ACK 는 오지만 P0004 에 반영 안 됨, %PW4 워드쓰기는 반영됨 -> (워드, 비트) 로 접근.
P44, P45 = ("%PW4", 4), ("%PW4", 5)
P04, P05 = ("%PW0", 4), ("%PW0", 5)
D_CODE = "%DW1000"
OUTPUTS = {"P44 (->로봇 DI0)": P44, "P45 (->로봇 DI1)": P45}
INPUTS = {"P04 (<-로봇 DO0)": P04, "P05 (<-로봇 DO1)": P05}

ENQ, EOT, ACK, NAK, ETX = b"\x05", b"\x04", b"\x06", b"\x15", b"\x03"


def frame_read(var):
    return ENQ + f"{STATION:02X}RSS01{len(var):02X}{var}".encode() + EOT


def frame_write(var, value, width=2):
    """width: 비트=2, 워드=4 (hex 자리수)."""
    return ENQ + f"{STATION:02X}WSS01{len(var):02X}{var}{int(value):0{width}X}".encode() + EOT


def parse(resp):
    """응답 프레임 -> 읽기면 비트값(int), 쓰기면 None."""
    if resp[:1] == NAK:
        raise IOError(f"PLC NAK (에러코드 확인): {resp!r}")
    if resp[:1] != ACK or resp[-1:] != ETX:
        raise IOError(f"응답 없음/형식 오류 (배선·국번·통신속도 확인): {resp!r}")
    body = resp[1:-1].decode()  # 국번(2) 명령(1) SS(2) [블록수(2) 바이트수(2) 데이터]
    return int(body[9:], 16) if body[2] == "R" else None


class CnetPLC:
    def __init__(self, port):
        import serial
        self.ser = serial.Serial(port, BAUD, timeout=1)
        self.lock = asyncio.Lock()  # 시리얼 한 줄을 두 작업이 같이 쓰므로 요청-응답 단위로 잠금

    def _xfer(self, frame):
        self.ser.reset_input_buffer()
        self.ser.write(frame)
        return parse(self.ser.read_until(ETX))

    def _set_bit(self, word, bit, on):
        v = self._xfer(frame_read(word))
        v = v | (1 << bit) if on else v & ~(1 << bit)
        self._xfer(frame_write(word, v, 4))

    async def _run(self, fn, *args, tries=3):
        async with self.lock:  # 읽고-고쳐-쓰기가 다른 요청과 섞이지 않게
            for i in range(tries):  # 485 응답이 가끔 한 번씩 빠짐(실측: 5분에 1회) -> 재시도
                try:
                    return await asyncio.to_thread(fn, *args)
                except IOError as e:
                    if i == tries - 1:
                        raise
                    log(f"통신 재시도 {i + 1}: {e}")

    async def read_bit(self, var):
        word, bit = var
        return (await self._run(self._xfer, frame_read(word)) >> bit) & 1

    async def write_bit(self, var, on):
        await self._run(self._set_bit, *var, on)

    async def write_word(self, var, value):
        await self._run(self._xfer, frame_write(var, value, 4))


class SimPLC:
    """가짜 PLC: 로봇이 DI0/DI1 을 받으면 DO0/DO1 로 그대로 돌려준다고 가정."""
    ECHO = {P04: P44, P05: P45}

    def __init__(self):
        self.bits = {}

    async def read_bit(self, var):
        return self.bits.get(self.ECHO.get(var, var), 0)

    async def write_bit(self, var, on):
        self.bits[var] = int(on)

    async def write_word(self, var, value):
        self.bits[var] = value


def log(msg):
    print(f"{time.strftime('%H:%M:%S')}  {msg}", flush=True)


async def output_test(plc):
    while True:
        for name, var in OUTPUTS.items():
            await plc.write_bit(var, True)
            log(f"OUT {name} ON   <- 펜던트 램프 확인")
            await asyncio.sleep(PULSE_S)
            await plc.write_bit(var, False)
            log(f"OUT {name} OFF")
            await asyncio.sleep(PULSE_S)


async def input_monitor(plc):
    last = {}
    while True:
        for name, var in INPUTS.items():
            v = await plc.read_bit(var)
            if last.get(name) != v:
                log(f"IN  {name} {last.get(name, '-')} -> {v}")
                last[name] = v
        await asyncio.sleep(POLL_S)


async def wait_for(plc, var, value, label):
    try:
        async with asyncio.timeout(ROBOT_TIMEOUT_S):
            while await plc.read_bit(var) != value:
                await asyncio.sleep(POLL_S)
    except TimeoutError:
        raise TimeoutError(f"로봇 응답 없음: {label} = {value} 을 {ROBOT_TIMEOUT_S:.0f}초 동안 못 받음") from None
    log(f"IN  {label} = {value}")


async def code(plc, value, msg):
    await plc.write_word(D_CODE, value)
    log(f"D01000 = {value}  {msg}")


async def out(plc, var, on, msg):
    await plc.write_bit(var, on)
    log(f"OUT {msg} {'ON' if on else 'OFF'}")


async def sequence(plc, cycles=None):
    n = 0
    while cycles is None or n < cycles:
        n += 1
        log(f"----- 사이클 {n}")
        await code(plc, 1111, "메인 시작")
        await out(plc, P44, True, "P44 (움직임1 요청)")
        await wait_for(plc, P04, 1, "P04 (움직임1 완료)")
        await code(plc, 2222, "움직임1 종료 확인")
        await out(plc, P44, False, "P44")
        await wait_for(plc, P04, 0, "P04")  # 로봇이 완료 신호를 내려야 다음 요청과 구분됨
        await asyncio.sleep(GRIP_S)
        await code(plc, 4444, "그리퍼1 종료 확인")
        await out(plc, P45, True, "P45 (움직임2 요청)")
        await wait_for(plc, P05, 1, "P05 (움직임2 완료)")
        await code(plc, 3333, "움직임2 종료 확인")
        await out(plc, P45, False, "P45")
        await wait_for(plc, P05, 0, "P05")
        await asyncio.sleep(GRIP_S)
        await code(plc, 5555, "그리퍼2 종료 확인")


async def main(plc, seq=False):
    try:
        if seq:
            await sequence(plc)
        else:
            await asyncio.gather(output_test(plc), input_monitor(plc))
    except TimeoutError as e:
        log(str(e))
    finally:
        for var in OUTPUTS.values():
            try:
                await plc.write_bit(var, False)
            except Exception as e:
                log(f"출력 OFF 실패 {var}: {e}  <- 직접 확인 필요")
        log("종료: 출력 OFF")


def selftest():
    assert frame_read("%PX4") == b"\x0500RSS0104%PX4\x04"
    assert frame_write("%PX44", True) == b"\x0500WSS0105%PX4401\x04"
    assert frame_write("%DW1000", 1111, 4) == b"\x0500WSS0107%DW10000457\x04"
    assert parse(b"\x0600RSS010101\x03") == 1
    assert parse(b"\x0600RSS010100\x03") == 0
    assert parse(b"\x0600WSS\x03") is None
    for bad in (b"", b"\x1500RSS1132\x03"):
        try:
            parse(bad)
        except IOError:
            continue
        raise AssertionError(bad)
    sim = SimPLC()
    asyncio.run(sim.write_bit(P44, True))
    assert asyncio.run(sim.read_bit(P04)) == 1

    class FakeSerialPLC(CnetPLC):
        """워드 하나를 흉내내서 읽고-고쳐-쓰기가 다른 비트를 보존하는지 확인."""
        def __init__(self):
            self.lock, self.word = asyncio.Lock(), 0x8001

        def _xfer(self, frame):
            body = frame[1:-1].decode()
            if body[2] == "R":
                return self.word
            self.word = int(body[-4:], 16)
    f = FakeSerialPLC()
    asyncio.run(f.write_bit(P45, True))
    assert f.word == 0x8021 and asyncio.run(f.read_bit(P45)) == 1
    asyncio.run(f.write_bit(P45, False))
    assert f.word == 0x8001
    global GRIP_S, ROBOT_TIMEOUT_S
    GRIP_S = 0
    sim = SimPLC()
    asyncio.run(sequence(sim, cycles=1))
    assert sim.bits[D_CODE] == 5555 and sim.bits[P44] == 0 and sim.bits[P45] == 0
    ROBOT_TIMEOUT_S = 0.2

    class DeadRobot(SimPLC):
        async def read_bit(self, var):
            return 0
    try:
        asyncio.run(sequence(DeadRobot(), cycles=1))
        raise AssertionError("timeout expected")
    except TimeoutError:
        pass
    print("selftest OK")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default=PORT)
    ap.add_argument("--sim", action="store_true")
    ap.add_argument("--seq", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        selftest()
    else:
        try:
            asyncio.run(main(SimPLC() if a.sim else CnetPLC(a.port), a.seq))
        except KeyboardInterrupt:
            pass
