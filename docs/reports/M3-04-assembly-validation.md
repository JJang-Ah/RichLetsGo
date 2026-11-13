# M3-04 조립 검증 기록

구현 PR #72. 승인된 `docs/tasks/M3-04-interpretation.md`를 기준으로 (a) P3, (b) 정산 시각 펀딩, (c) 조립을 분리했다. Python 3.14.3 / Windows에서 합성 입력·가짜 시계·가상 거래소·임시 SQLite만 사용했다. 실제 키/계좌/수집 DB/미관측 판정 풀은 접근하지 않았다.

## 커밋 단위와 검사

| 단위 | 커밋 | 범위 | 단위 검증 |
|---|---|---|---|
| a | `08c6b58` | #66 P3 4건·#67 P3 8건의 저장소/제어 변경. 실제 tick 배선은 c | 저장소·제어·보호·대시보드·P3 회귀 121 passed / 14.26초, lint/format/mypy |
| b | `b36b4d2` | settled_at·정산 보유량 스냅샷·실제 체결 배분 delta·지연/미배분 펀딩 | 기존 백테스트/포트폴리오와 신규 정산 회귀 169 passed / 9.85초, mypy |
| c | 이 문서를 포함한 조립 커밋 | 실행 진입점·D97·경계/정기 작업·복구·health | 조립·입력·진입점·설정·백테스트·재시작/저널/D90/추적 손절 296 passed / 100.04초, lint/format/mypy |

후속 최초 계좌 입금/잠금 대기 시세 만료 회귀까지 전체 **1544 passed / 187.73초**. 마지막 전송 직전·marker 대기 뒤 준비/수동 정지 재검사는 위 296건 묶음으로 추가 확인했다. 정기 REST 대조 때 준비 게이트를 먼저 닫는 마지막 배선 뒤 조립 회귀 37 passed / 8.76초. 지원 버전 Python 3.12/3.14의 최종 커밋 CI 결과는 PR에 별도로 남긴다. FastAPI TestClient의 기존 StarletteDeprecationWarning 1개는 실패가 아니다.

검증 명령:

```powershell
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
.\.venv\Scripts\python.exe -m mypy
.\.venv\Scripts\python.exe -m pytest tests/test_m304_trader.py tests/test_m304_inputs.py tests/test_m304_app.py tests/test_service_config.py tests/test_backtest.py -q --tb=short
.\.venv\Scripts\python.exe -m pytest -q --tb=short --durations=15
.\.venv\Scripts\python.exe tools/m304_compare_boundary.py --baseline b36b4d2
```

## 완료 조건 대조

| 조건 | 합성 근거 |
|---|---|
| 자정 네 봉 24순열, 중복 봉, S1→S2/BTC→ETH | `test_virtual_exchange_all_midnight_permutations_same_decisions_and_orders`: 실제 게이트/포트폴리오/동기화기/가상 거래소를 거쳐 결정 4개·동일 주문, 재전달 후 주문 수 불변. 공통 `boundary_signals` 순서 및 S2 끔 별도 검사 |
| REST 보충/실패의 계약별 생략, 다음 T 선도착 | `test_bar_timeout_only_skips_missing_symbol`, `test_next_boundary_arrives_first_and_account_prepare_before_commit_resamples`. 누락 계약 data 정지·알림. 이력 불연속도 계약별로 생략 |
| 단계별 커밋 전/후 장애 | `test_each_boundary_commit_replays_same_output`: 입력/체결/펀딩/준비/자정 반영/출력/위험 결정/종료 체크포인트 × 전/후 16경우. 새 동기화기로 다시 부팅, 출력/기준/결정 4개·계약별 주문 2개 유지 |
| 준비 레코드 불변, 결과 불명 판정 | 준비 전 중단은 새 평가액으로 재촬영, 성공 뒤 중단은 이전 준비 재사용. `test_sqlite_prepare_unknown_uses_marker_before_resampling`: 실제 SQLite COMMIT/after_commit 장애에서 marker 판정 후 롤백만 재촬영 |
| 최신 위험 기준, 기존 결정 재사용 | `test_undecided_signal_uses_new_equity_and_pause_and_reuses_decision`: 출력 뒤 손실·수동 정지를 넣으면 새 결정에 최신 평가액/정지 저장, 기존 결정은 변경 안 함 |
| 위험 결정/전송의 결과 불명 | 실제 SQLite 적용/롤백 양쪽에서 `test_risk_decision_unknown_resolves_without_duplicate_entry`, `test_live_sqlite_entry_unknown_reconciles_then_sends_exactly_once`. 실제 재동기화의 일시적 시스템 정지 변경이 자정 publish를 막지 않는 회귀 포함 |
| 전송 중 재시작·저널/D90/추적 손절 | 기존 `tests/v0/test_recovery_integration.py`, `test_sqlite_protection.py`, `test_trailing_stop.py` 유지. fresh 출력에 연결된 CREATED만 조립층에서 복원하며 SUBMITTED/UNKNOWN은 기존 M1 규칙 |
| 정산 시각 배분, 전량 청산 뒤 도착, 0 보유, 거래일 | `test_m304_funding.py` memory/SQLite 검사. T 이후 체결의 실제 배분 delta 제거, 미귀속 체결 대기, 지연 비용 S1 귀속, 미배분 outbox, 자정 전날 귀속 |
| 자정 펀딩 대기와 보호·제어 | `test_funding_wait_keeps_protection_and_control_tick_running`: 모든 계약 진입 닫힘, sync→control tick 계속, 거래일 변경은 기록 도착 이후. 최초 추적기도 펀딩 확인 전 만들지 않음 |
| D97 | `test_service_config.py`: primary FAIL/INDETERMINATE + secondary PASS 거부, 등록/해시/종목/기간/선택 쌍 불일치 거부, S2 false 단독 허용·그 외 필드/부 보고서 거부 |
| 준비 실패/재연결 | 실패한 부팅/재연결 뒤 정기 대조만 성공해도 준비 게이트가 열리지 않음. 일일 기준 미완료/개인 WS 끊김도 신규 진입 차단 |
| D62·입력 경계 | 구성한 REST 응답으로 양수 liqPrice 변환·부등식 위반 시 실제 CloseIntent/EXITING, 펀딩 부호 반전, 고정 시간 봉 범위·미완성 봉 거부 |
| health/종료 | TestClient로 Host/Bearer 검사·정보 제한·0.0.0.0 거부. Trader가 종료 신호를 소유하며 Uvicorn이 덮어쓰지 않음. 기존 SQLite 취소 시점별 UoW 회수 검사 유지 |

## 백테스트 공통 함수 추출 비교

`strategy/boundary.py`의 `boundary_steps`와 `boundary_signals`를 백테스트 실행기와 Trader가 함께 호출한다. 기존 백테스트 테스트 파일은 수정하지 않았다.

`tools/m304_compare_boundary.py --baseline b36b4d2`는 이전 커밋의 **runner.py**와 현 runner를 별도 프로세스로 실행한다. 나머지 패키지·합성 fixture는 양쪽에 동일하게 주입해 추출 자체의 영향을 비교한다. `test_walkforward.fixture_config`의 단일 격자/두 달 합성 캠페인(S1A-S2A/S1A-S2B/S1B-S2A/S1B-S2B)을 사용한다. 소스 DB와 모든 보고서는 TemporaryDirectory에 만들고 프로세스 종료 후 삭제한다.

첫 측정: 전 10.373초 / 후 10.701초. 두 보고서의 정규 JSON 본문+LF는 모두 **673,239 bytes**, 바이트 일치이며 `codec.digest`는 모두:

```text
f4840f63a8d685413261554fdab2e7e4c59548576ebda98cd4e7b75c8e401931
```

게시 재현 도구로 다시 실행한 측정은 전 12.631초 / 후 12.485초이며 같은 바이트 수·digest를 확인했다.

48시간 단일 합성 실행도 변동 executed_at만 제외한 본문 36,971 bytes와 digest `dc1494cca92ac66356b2ff5ee16de7bda3ccafeb859be744d914b9ff2fe5d74e`가 추출 전후 같았다. 실행 시간은 비교 본문/해시에 포함하지 않는다.

## 실행 경계와 남은 수동 확인

- V002는 command_identities만 담는다. 펀딩 settled_at과 경계 입력·출력·스냅샷·config_versions는 기존 V001 JSON payload 테이블로 투영한다.
- entrypoint는 demo/AI disabled만 허용한다. 최초 추적기는 펀딩 이후의 준비 스냅샷으로 초기화하며, 그 이전 손익은 과거 기준으로 추정하지 않는다. 기존 추적기는 첫 관측 전에 복원한다.
- 별도 경계 작업이 REST/자정 펀딩을 기다려도 정기 작업은 보호/취소/제어 tick을 계속한다. REST 이력은 6일23시간으로 잡아 Bybit 7일 범위에서 통신 시간을 확보한다. 범위 밖 워터마크는 자동 추정하지 않는다.
- **실제 24시간 데모·자정 1회·실제 계좌의 부착 손절/liqPrice 응답 확인은 미실행**이다. 서비스 설치·텔레그램 수신 폴러는 후속 M3-05/06 범위이며 시작 알림과 공통 명령 처리만 연결했다. PASS V1 전략 사용자 선택과 M3-07 판정을 대신하지 않는다.
- D103 수집 DB·통계·판정 자료와 수집 프로세스는 접근하지 않았다.
