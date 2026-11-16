# M3-05 단위 (a) 검증 기록

- 기준: PR #73 해석 승인, main e1823ae. 브랜치 `task/M3-05-telegram`.
- 범위: PR #72 이월 다섯 건. Telegram 본 작업 (b)는 시작하지 않았으며 Claude의 중간 검토 대상이다.
- 입력: 합성 데이터·가짜 시계·가상 거래소·임시 SQLite/저널만 사용했다. 실 Bot/키/연구 DB/판정 자료/D103 실행 폴더·프로세스에는 접근하지 않았다.

## 변경과 근거

| 항목 | 구현 | 검증 |
|---|---|---|
| 정산 정시 검증 | 엄격 펀딩 배분과 Trader에서 UTC 정시 여부 검사. 반올림 없이 어긋난 원시 시각/금액을 pending_funding에 보존하고 경고. funding_seen/received를 만들지 않음. 유효 정산 적용 뒤 해당 pending 항목 archive | 초/마이크로초 오차 거절, 자정 보유 펀딩 대기 유지, 보호 tick 진행, 이후 정시 지급 배분 성공. 기존 거래일 귀속 테스트의 ±1초 입력은 유효한 ±1시간으로 변경 |
| 반복 경고 | notify.batching.WarningBatcher를 로컬 로그에 연결. 원인별 최초 즉시·고정 창 반복 횟수/최신 문구/시각 요약·해소·재발·유휴 정리. SQLite worker/main 동시 호출을 RLock으로 보호 | 1초 간격 60회 → 최초+반복 59회 요약, 다른 원인 보존/해소/재발, 스레드 200회 → 최초+반복 199회 요약 |
| 불변 레코드 | config_version/boundary_input/output/position_snapshot/funding_record의 다른 값 덮어쓰기를 저장 API에서 거절. 불변 SQL 투영은 insert 또는 같은 payload 무변경, 충돌은 StorageError. checkpoint/진행 상태 변경은 허용 | 메모리·SQLite 동일값 재생/아카이브 뒤 충돌/부수 변경 롤백, SQL 감사 투영 자체 충돌/created_at 불변 |
| 주문 디코드 | 복구 대상 전략·계약을 SQL JSON 조건으로 거른 뒤 과거 종결 주문 디코드. 명시적 참조 조회 유지 | 관련 과거 종결 주문 1건 유지, 무관한 과거 종결 주문 300건에서 디코드 0건. 전체 계좌 SQL 스캔은 남으며 새 색인 효과로 주장하지 않음 |
| 준비 게이트 경합 | pending entry/경계 커밋/entry_paused 복구를 boundary_lock으로 직렬화하고 내부에서 상태 재검사. 복구할 일이 없으면 잠금을 기다리지 않아 보호/제어 tick 진행 보존 | 3경로 × 복구/경계 선행 × 커밋 적용/롤백 12조합에서 신호 4개 ADMITTED 유지. 복구 실패 시 경계/신호 미결정으로 대기, 성공 뒤 새 운영자 정지 보존 |

## 실행 결과

운영 Python 3.14에서 실행했다. 지원 Python 3.12·3.14 CI는 push 뒤 별도로 확인한다.

- 전체 회귀: `python -m pytest -q --tb=short` → **1,575 passed**, 259.10초. 마지막 스레드/복구 실패 회귀 2건 추가 전 수집한 전체 실행이다.
- 최종 코드의 관련 회귀: `python -m pytest tests/test_m305_unit_a.py tests/test_m304_trader.py tests/test_m304_funding.py tests/test_m304_p3.py tests/test_m304_app.py tests/test_sqlite_storage.py -q --tb=short` → **114 passed**, 8.75초. 신규 29건 포함.
- `ruff check .`, `ruff format --check .`, `mypy`, `git diff --check` 통과. mypy 153 source files.
- 기존 Starlette TestClient의 httpx 사용 중단 예정 경고 1건 외 실패 없음. 테스트 정리에서 자신이 만든 coroutine/스레드만 회수했다.

## 운영 설정·후속 작업

- `[trader].warning_window_seconds` 양수 정수 설정을 명시해야 한다. 예시는 60초이며 런타임 설정 로더는 생략을 거절한다. README와 config.example.toml을 맞췄다.
- V001/V002 수정 또는 V003 추가 없음. Q1의 api_expires_at 필수·Q2 후속 수동 복구 명세 분리·Q3 개인 채팅·Q4 보수적 커서·AI 비용 비활성은 승인 내용으로 M3-05 명세에 반영했다.
- (b)에서 Bot 수신/발송 worker·공통 명령·영속 묶음/outbox를 구현한다. 완료 inbox/outbox archive(active=0), 미처리 항목만 활성 작업 집합, 토큰/키 비저장·비출력 조건을 유지한다. 단위 (a)의 로컬 경고 묶음은 아직 영속 Bot outbox가 아니다.
- 실제 봇 수동 확인은 전체 합성 검증 뒤 사용자와 함께 수행한다. 이번 기록은 실제 운영 완료 판정이 아니다.
