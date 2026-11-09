# PLC 비동기·동기 통신 실습 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (사용자가 Native 선택) to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 같은 PLC 감시·값 변경 기능을 `plc_project.py`(비동기)와 `plc_project_sync.py`(단일 흐름 동기)로 구현하고, 무응답 대기 중 차이를 기록으로 비교할 수 있게 한다.

**Architecture:** 판단 로직은 입출력 없는 `Monitor`에 두어 두 판이 같은 원칙을 쓴다. 두 프로그램은 "키 확인·표시·통신을 어떻게 번갈아 하는가"만 다르다. 비동기판은 세 코루틴, 동기판은 한 루프다.

**Tech Stack:** Python 3.14, PyModbus 3.16.1, pyserial 3.5, `msvcrt`, `unittest`

**Spec:** `docs/spec.md`

## Global Constraints

- 프로젝트 폴더는 `C:\Users\128\orca\workspaces\비동기\sunstar\plc_modbus`다 (원본 `C:\Users\128\dev\Project4_day1_과제2`에서 복사). 커밋하지 않는다(사용자 결정). 폴더는 git 저장소가 아니다.
- 설정 기본값: `port=COM5, baudrate=9600, bytesize=8, parity=N, stopbits=1, station=1, timeout=2.0, retries=0, poll_interval=1.0`
- 요청 주소는 항상 0이다. 읽기는 FC03 수량 1, 쓰기는 FC06, 값은 0~65535다.
- 국번은 1과 2만 허용한다. 0은 항상 거절한다.
- `plc_project_sync.py`와 그것이 가져오는 코드에 스레드·프로세스·asyncio를 쓰지 않는다. 시험 서버(`tests/`)는 예외다.
- 실패한 읽기는 값으로 표시하지 않는다. 재시도하지 않는다.
- 자동 테스트와 실제 PLC 결과를 구분해 기록하고, PLC로 실행 전 항목은 "미확인"으로 둔다.

## 계획 단계 사전 확인 결과

- `socket://127.0.0.1:<port>` URL로 동기·비동기 클라이언트 모두 RTU 프레임 읽기·쓰기가 동작한다(직접 실행해 확인).
- PyModbus 시험 서버는 없는 국번에 예외 응답(`exception_code=4`)을 보내 무응답을 재현하지 못한다. 그래서 `tests/rtu_server.py`를 직접 쓴다.
- 비동기 클라이언트는 연결이 없으면 `await` 전 호출 시점에 `ConnectionException`을 던진다. 호출식 전체를 `try` 안에 둔다.
- 쓰기 응답의 값은 `reply.registers[0]`로 돌아온다.

## Review Focus

1. 통신 중 쌓인 키: 동기판에서 통신 중 입력한 줄이 반환 직후 빠짐없이 순서대로 접수된다.
2. 포트가 실행 중 끊김: 예외로 프로그램이 죽지 않고 실패가 기록되며, 재연결되면 관측이 다시 이어진다.
3. 국번 2 시작: 한 번도 성공하지 않으면 `미관측`이고 값 필드가 어디에도 없다.
4. 응답 대기 중 `quit`: 비동기판은 timeout 전에, 동기판은 반환 뒤 종료하고 어느 쪽이든 포트를 닫는다.
5. 입력 편집과 잘못된 값: 백스페이스, 빈 줄, `write 65536`, `write -1`, `write 1.5`, `station 0`, 전각·한글 입력에서 죽지 않고 통신도 보내지 않는다.

---

### Task 1: 공통 코어 `plc_common.py`

**Files:**
- Create: `plc_config.json`, `plc_common.py`
- Test: `tests/test_common.py`

**Interfaces:**
- Produces:
  - `load_config(path) -> Config`, `Config.client_kwargs() -> dict`(`baudrate, bytesize, parity, stopbits, timeout, retries`), `parse_args(argv) -> Config`(`--station`, `--config`)
  - `parse_command(line) -> Command`(`kind`: write·station·status·quit, `value`), 잘못되면 `ValueError(사유)`
  - `EventLog(path, stream)`: `record(type, **fields) -> dict`, `new_log_path(mode) -> Path`
  - `KeyLines(source)`: `poll() -> list[str]`. `source`는 `kbhit()`, `getwch()`를 가진 객체이고 기본값은 `msvcrt`. 키마다 `key_seen` 기록. Ctrl+C(`\x03`)는 `quit` 줄로 낸다
  - `Monitor(config, log, clock=time.monotonic)`:
    - `accept_line(line) -> bool` (`quit`이면 참)
    - `next_comm() -> Comm | None`, `Comm.kind`는 read·write, `Comm.station`, `Comm.value`
    - `finish_read(comm, value=None, error=None)`, `finish_write(comm, ok, error=None)`
    - `heartbeat_due() -> bool`, `heartbeat()`, `show_status()`
  - `read_value(reply) -> int`, `check_write(reply, value)`: 예외 응답·개수 이상이면 `ValueError`
  - `COMM_ERRORS = (ModbusException, OSError, ValueError)`

- [ ] **Step 1: 실패하는 테스트 작성** — 아래 이름과 단언을 코드로 쓴다.
  - `test_config_defaults`: 위 기본값 전부와 `--station 2` 우선 적용
  - `test_parse_valid`, `test_parse_rejects`: `write 0`, `write 65535` 통과. `write 65536`, `write -1`, `write 1.5`, `write`, `station 0`, `station 3`, 빈 줄, `쓰기 100`은 `ValueError`
  - `test_keylines_edit`: 글자, 백스페이스, Enter, CR·LF, 특수키 접두 `\xe0`·`\x00` 뒤 한 글자 무시, `\x03`→`quit`
  - `test_turn_order`: `write 100`, `write 200` 접수 후 `next_comm` 순서가 write 100 → (성공) read → write 200 → (성공) read
  - `test_failed_write_no_readback`: 실패한 쓰기 뒤 주기 전이면 다음은 대기열 항목이다
  - `test_read_not_starved`: 쓰기가 계속 실패해도 주기가 지나면 요청 사이에 read가 끼어든다
  - `test_period_no_catchup`: 주기를 5배 넘겨도 read는 한 번만 나온다
  - `test_station_in_order`: `write 100`, `station 2`, `write 200`의 국번이 1, 2다
  - `test_last_good_kept`: 읽기 성공 후 실패하면 마지막 관측은 그대로이고 `read_failed`에 `value` 키가 없다
  - `test_unobserved`: 성공이 없으면 `show_status` 기록에 `미관측`이 있고 값 필드가 없다
  - `test_observation_matches`: 쓰기 성공 뒤 관측에 `expected=100`, 값에 따라 `matches` 참·거짓. 그다음 관측에는 `expected`가 없다
  - `test_read_value_rejects`: `isError()` 참, `registers` 길이 0·2는 `ValueError`
  - `test_write_echo_mismatch`: 응답 값이 쓴 값과 다르면 `check_write`가 `ValueError`
- [ ] **Step 2: 실행해 실패 확인** — `python -m unittest tests.test_common -v`. Expected: `plc_common` 없음으로 실패
- [ ] **Step 3: 구현** — 시계는 주입하고, `Monitor`는 소켓·`print`를 쓰지 않는다(출력은 `EventLog` 담당). `EventLog`는 줄마다 flush한다. 사람이 읽는 터미널 줄과 JSONL을 함께 쓴다.
- [ ] **Step 4: 실행해 통과 확인** — 같은 명령. Expected: 전부 통과

### Task 2: 시험 서버와 동기판 `plc_project_sync.py`

**Files:**
- Create: `tests/rtu_server.py`, `tests/helpers.py`, `plc_project_sync.py`
- Test: `tests/test_sync.py`

**Interfaces:**
- Consumes: Task 1 전부
- Produces:
  - `plc_project_sync.run(config, log, keys) -> int` (종료 코드), `main()`
  - `tests/rtu_server.py`: `RtuServer(stations={1}, value=0)` 컨텍스트 관리자. `.url`, `.value`, `.requests`(받은 프레임 기록), `.drop_clients()`. 스레드 기반, CRC 검사, FC03·FC06만 처리, 없는 국번에는 **아무것도 보내지 않음**
  - `tests/helpers.py`: `ScriptedKeys([(시각, 문자열)])` (`kbhit`, `getwch`, 시각 전에는 키 없음. 시각은 조건 함수도 허용), `test_config(url, **over)`, `of_type(events, type)`. 키의 기본 끝에 `quit`을 넣어 테스트가 멈추지 않게 한다

- [ ] **Step 1: 실패하는 테스트 작성** (`timeout=0.5`, `poll_interval=0.2`)
  - `test_server_silent_for_missing_station`: 서버 자체 확인. 국번 2 프레임에 응답 없음, 국번 1은 CRC 맞는 응답
  - `test_write_then_read`: `write 100`, `write 200`. `write_result` 2회 성공, 각 뒤 `observation`의 `value`·`matches` 참, `request_accepted`→`write_result`→`observation` 순서, 서버 값 200
  - `test_no_response_read_failed`: `station 2` 후 `read_failed`가 생기고 소요 시간이 약 timeout, 마지막 관측은 유지되고 새 `observation`이 없다
  - `test_input_delayed_during_wait`: 국번 2 `comm_start` 직후 키 입력. 그 `comm`의 `comm_end` 전에는 `key_seen`·`request_accepted`·`heartbeat`가 **없다**. 반환 뒤 입력 줄이 순서대로 접수된다(Review Focus 1)
  - `test_start_station2_unobserved`: 국번 2로 시작하면 `observation` 0건, `status_shown`에 `미관측` (Review Focus 3)
  - `test_restore_station1`: 2→1 복원 후 새 `observation`, 시각 갱신, 다시 `write` 후 재조회 일치. 국번 2에 대기하던 쓰기는 한 번만 시도되고 재전송 없음(`.requests`로 확인)
  - `test_quit_after_return`: 대기 중 `quit` → 종료 시각이 `comm_end` 뒤. 다음 실행에서 같은 URL 재연결 성공(포트 닫힘 확인)(Review Focus 4)
  - `test_server_drop_survives`: `drop_clients()` 뒤 실패가 기록되고 프로그램이 살아 있으며, 뒤이어 `observation`이 다시 생긴다(Review Focus 2)
  - `test_bad_input_sends_nothing`: Review Focus 5의 입력들로 `.requests`에 쓰기 없음, `request_rejected` 건수 일치
  - `test_open_failure_exit_1`: 닫힌 포트면 종료 코드 1
  - `test_no_concurrency_imports`: `plc_project_sync.py`와 `plc_common.py`를 `ast`로 검사해 `asyncio, threading, multiprocessing, subprocess, concurrent`가 없다
- [ ] **Step 2: 실행해 실패 확인** — `python -m unittest tests.test_sync -v`. Expected: 서버 테스트 외 `plc_project_sync` 없음으로 실패. 서버 테스트는 서버 구현 뒤 먼저 통과시킨다
- [ ] **Step 3: 구현** — 한 루프에서 `keys.poll()` → `accept_line` → `heartbeat` → `next_comm` → 통신 → 결과 반영. 할 일이 없으면 0.02초 `time.sleep`. 통신 전후에 `comm_start`·`comm_end`를 기록하고 호출식 전체를 `except COMM_ERRORS`로 감싼다. `KeyboardInterrupt`는 `quit`처럼 처리하고, 포트는 `finally`에서 닫는다.
- [ ] **Step 4: 실행해 통과 확인** — `python -m unittest tests.test_common tests.test_sync -v`. Expected: 전부 통과

### Task 3: 비동기판 `plc_project.py`

**Files:**
- Create: `plc_project.py`
- Test: `tests/test_async.py`

**Interfaces:**
- Consumes: Task 1 전부, Task 2의 `tests/rtu_server.py`·`helpers.py`
- Produces: `plc_project.main_async(config, log, keys) -> int`, `main()`

- [ ] **Step 1: 실패하는 테스트 작성** — Task 2의 시나리오를 같은 입력·같은 설정으로 쓴다(`test_write_then_read`, `test_no_response_read_failed`, `test_start_station2_unobserved`, `test_restore_station1`, `test_server_drop_survives`, `test_bad_input_sends_nothing`, `test_open_failure_exit_1`). 판 차이 단언은 아래다.
  - `test_input_during_wait`: 국번 2 `comm_start` 직후 `status`와 `write 300`. `key_seen`, `status_shown`, `request_accepted`가 모두 같은 `comm`의 `comm_end` **전**에 있다
  - `test_heartbeat_during_wait`: 무응답 대기 중 `heartbeat` 간격 최댓값이 표시 주기(0.5초)의 2배 미만이다
  - `test_one_request_at_a_time`: 키를 몰아 넣어도 `comm_start`와 `comm_end`가 번갈아 나오고 겹치지 않는다
  - `test_quit_during_wait`: 대기 중 `quit` → timeout 전에 종료하고 같은 URL 재연결 성공(Review Focus 4)
  - `test_observation_only_server_value`: 대기열을 쌓아도 `observation`의 값은 서버 값과만 일치한다(실패값·입력값이 관측으로 안 나옴)
- [ ] **Step 2: 실행해 실패 확인** — `python -m unittest tests.test_async -v`. Expected: `plc_project` 없음으로 실패
- [ ] **Step 3: 구현** — 코루틴 셋: 입력(0.05초마다 `keys.poll`), 표시(0.5초마다 `heartbeat`, 대기 중이면 경과 안내), 통신(`next_comm` 실행. 없으면 0.02초 `asyncio.sleep`). `quit`이면 나머지를 취소하고 `finally`에서 포트를 닫는다. `CancelledError`·`KeyboardInterrupt`는 삼키지 않는다.
- [ ] **Step 4: 실행해 통과 확인** — `python -m unittest discover -s tests -t . -v`. Expected: 전부 통과

### Task 4: 실행 안내, 기록 양식, 로그 요약

**Files:**
- Create: `tools/summarize_log.py`, `tests/test_summarize.py`, `docs/run_guide.md`, `docs/test_record.md`, `docs/comparison.md`

**Interfaces:**
- Consumes: 로그 형식(spec 6절)
- Produces: `summarize(events) -> dict`, `python tools\summarize_log.py <로그>`

- [ ] **Step 1: 실패하는 테스트 작성** — `test_summary_fields`: 손으로 만든 이벤트로 `heartbeat` 최대 간격, 각 `comm`의 소요 시간과 그 안에서 접수된 입력 수, 반환 뒤 첫 `key_seen`까지 시간을 단언한다.
- [ ] **Step 2: 실행해 실패 확인** — `python -m unittest tests.test_summarize -v`. Expected: 모듈 없음으로 실패
- [ ] **Step 3: 구현과 문서 작성**
  - `run_guide.md`: 시작 전 확인(QModMaster 해제, 두 프로그램 동시 실행 금지, 강사 확인), 실행 명령, 세 시험의 입력 순서를 비동기판·동기판 각각 적는다.
  - `test_record.md`: 과제의 표. 자동 테스트 결과는 "로컬 시험 서버"로 표시하고, 실제 PLC 칸은 전부 "미확인"으로 둔다.
  - `comparison.md`: 과제의 비교 질문 3개. 로컬 시험 서버의 실측 수치를 근거로 초안을 쓰고, PLC 결과로 교체할 자리를 표시한다.
- [ ] **Step 4: 통합 확인** — 전체 테스트를 실행하고, 시험 서버로 두 프로그램의 로그를 실제로 만들어 `summarize_log.py` 출력을 `comparison.md`에 옮긴다. Expected: 전체 통과, 동기판 입력 지연과 비동기판 즉시 접수가 수치로 나옴
- [ ] **Step 5: 최종 검토** — 새 컨텍스트의 리뷰어가 spec·plan·코드를 대조한다. Critical·Important는 테스트부터 고치고 전체 테스트를 재실행한다.
