# 실행 안내

## 실제 연결 설정

설정은 `plc_config.json` 하나에 있고 두 프로그램이 같은 파일을 읽는다.

| 항목 | 값 | 확인 상태 |
| --- | --- | --- |
| COM | COM5 (CP210x) | 사용자 확인 |
| 형식 | 9600, 8N1 | 실제 PLC 응답으로 확인. XG5000 대조 필요 (8E1은 무응답이었음) |
| 국번 | 1 (시험 둘에서 2) | TASKS.md |
| 주소 | 요청 주소 0 = D01000. 코드에 고정 | TASKS.md |
| timeout, retries | 2.0초, 0회 | 과제가 허용한 시험용 값. 세 시험과 두 판 모두 동일 |
| 감시 주기 | 1.0초 | spec |
| PyModbus | 3.16.1 | 이 PC에서 확인 |

## 시작 전 확인

0. 처음 실행하는 PC라면 설치한다: `python -m pip install "pymodbus[serial]==3.16.1"`
1. 강사에게 확인: D01000이 기존 래더·출력 제어에서 안 쓰이는지, 실습선에 국번 2 장치가 없는지.
2. QModMaster 연결을 해제한다. 두 프로그램을 동시에 실행하지 않는다.
3. 프로젝트 폴더의 터미널에서 실행한다.

## 명령

```text
write <0~65535>   D01000 쓰기 요청
station <1|2>     요청 대상 국번 변경
status            현재 상태 표시
quit              종료 (Ctrl+C도 종료)
```

입력 글자는 프로그램이 키를 읽었을 때 화면에 찍힌다. 터미널 첫 줄에 이번 실행의 기록 파일 경로가 나온다.

## 시험 순서

비동기판(`python plc_project.py`)으로 전부 한 뒤, 종료하고 동기판(`python plc_project_sync.py`)으로 같은 순서를 반복한다.

**시험 하나: 정상 감시와 값 변경**
1. `write 0`으로 시작 값을 맞추고, `관측` 줄이 약 1초마다 나오는지 본다.
2. `write 100` 입력 → `요청 접수`, `쓰기 결과 ... ok=True`, `관측 value=100 station=1 expected=100 matches=True`를 확인한다.
3. `write 200`으로 반복한다. XG5000에서 D01000 값도 함께 확인한다.

**시험 둘: 응답 없는 대상 요청**
1. `station 2`를 입력한다. `통신 시작 kind=read station=2`가 나오고 약 2초 뒤 `읽기 실패`가 나온다.
2. **`통신 시작` 줄을 본 직후** `status`를 입력한다. 글자가 언제 화면에 나타나는지, 응답이 언제 나오는지 본다.
3. 다음 `통신 시작` 직후 `write 300`을 입력한다. 국번 2로 전송되므로 실패가 정상이고, PLC 값은 200으로 남아야 한다.
4. `status`로 `last_read`가 실패이고 `observed_value=200`, `observed_station=1`이 유지되는지 확인한다.

**시험 셋: 정상 대상 복원**
1. `station 1`을 입력한다.
2. 새 `관측`의 값과 시각이 갱신되는지 확인한다.
3. `write 400` 입력 → `matches=True`와 XG5000 값을 확인한 뒤 `quit`으로 종료한다.

**미관측 확인 (한 판당 1회)**
`python plc_project.py --station 2` 또는 `python plc_project_sync.py --station 2`로 시작해 `status`를 입력하면 `observed=미관측`이어야 한다. 값 0이 보이면 오류다. `quit`으로 종료한다.

**추가 확인: 대기 중 Ctrl+C**
국번 2 대기 중 Ctrl+C를 눌러 두 판이 바로 끝나는지 본다. 실제 COM 포트의 동기판 동작은 아직 확인되지 않았다.

## 결과 보기

```text
python tools\summarize_log.py logs\<파일>.jsonl
```

0.5초 이상 걸린 통신마다 대기 중 처리한 키, 접수, 표시 횟수와 반환 뒤 첫 키까지의 시간을 보여 준다.

## 문제가 생기면

- 포트 열기 실패: `hint`를 보고 COM 번호와 다른 프로그램의 사용 여부를 확인한다.
- 국번 1에서도 계속 읽기 실패: 8N1, 9600, 배선 A/B를 XG5000과 대조하고, `plc_config.json`의 값이 PLC 설정과 같은지 확인한다.
- 사유에 `CLOSING CONNECTION`이 보여도 라이브러리 문구일 뿐이다. 직렬 포트는 닫히지 않으며, 무응답이 5회쯤 이어지면 이 문구로 바뀐다.

## 자동 테스트 (PLC 불필요)

```text
python -m unittest discover -s tests -t .
python tools\console_drive.py {async|sync}
python tools\console_drive.py {async|sync} --ctrl-c
```

`console_drive.py`는 숨겨진 콘솔에 실제 키 이벤트를 넣어 시험 서버와 통신한다. 실제 PLC 결과가 아니다. 로그는 `logs\`에 생긴다.
