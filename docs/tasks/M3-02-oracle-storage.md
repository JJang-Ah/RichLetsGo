# M3-02 Oracle 저장소

- 상태: Codex 검토 반영(2026-10-21), 재검토 대기
- 구현: Codex / 검토: Claude Code
- 브랜치: `task/M3-02-oracle-storage`
- 근거: 03 5장(주문 상태·저널)·6장(스키마·테이블·백업), D29, D44, D46, D60, D61, D74, M1-02·03·04 저장소 인터페이스

## 목표

M1의 `UnitOfWork` 프로토콜을 Oracle Database Free로 구현해, 메모리 구현과 **같은 의미**(원자적 커밋, 유일성, 롤백)를 보장한다. 단, 메모리 구현에는 없는 **커밋 결과 불명** 상황을 따로 다룬다.

## 사용자 준비 (Q23)

- Oracle Database Free 설치(관리자 권한), SQL Developer 설치.
- `RLG_DEMO`·`RLG_LIVE`·`RLG_READ` 계정 생성 스크립트(`db/admin/create_users.sql`)는 이 작업에서 제공하고, 실행은 사용자가 SYS로 한다. 비밀번호는 자격 증명 관리자에만 저장.

## 연결 방식 결정 (PR #15 검토 2번)

M1의 저장소 함수(`get/find/all/put/add`)는 **동기**이고 위험 게이트·OMS 훅·포트폴리오 서비스가 await 없이 부른다. 순수 로직을 바꾸지 않기 위해 다음 방식으로 연결한다.

1. **적재** — `__aenter__`에서 새 연결을 얻고 트랜잭션을 시작한 뒤, 계좌 잠금 행을 `SELECT … FOR UPDATE`로 잡는다. 잠금을 얻은 **뒤에** 그 계좌의 작업 집합(아래)을 비동기로 읽어 트랜잭션 내부 메모리 상태로 만든다. 잠금 전에 읽은 상태나 이전 트랜잭션의 메모리 상태를 다시 쓰지 않는다.
2. **조작** — 저장소 객체는 그 메모리 상태를 동기로 읽고 바꾼다. 바뀐 행을 추적한다(새 행 / 바뀐 행).
3. **반영** — `commit`에서 바뀐 행만 같은 연결·같은 트랜잭션으로 비동기 기록하고 COMMIT한다. 그 밖의 종료는 ROLLBACK. 연결은 커밋·롤백 후 반환한다.

**작업 집합** (계좌 단위로 적재하는 행). 메모리 구현도 같은 정의를 쓰도록 바꿔, M1 테스트가 작업 집합 규칙까지 검증하게 한다.

| 저장소 | 적재 범위 |
|---|---|
| orders | 종결되지 않은 주문, 미반영 체결이 있는 주문, 종결되지 않은 에피소드·청산 의도·보호 복구가 참조하는 주문, 최근 N일(설정) 주문 |
| fills | 귀속 보류·미반영 체결, 최근 N일 체결(exec_id 중복 판정용) |
| decisions | signal_id의 봉 시각이 최근 N일 안인 결정. **N일보다 오래된 봉의 신호는 위험 게이트가 거절**(중복 판정 범위 밖이므로) |
| episodes, close_intents, protection recovery, outbox | 종결되지 않은 것 전부 + 그것이 참조하는 것 |
| books, risk_states, portfolio 정지·회차 | 전부(계좌당 행 수가 작음) |

- exec_id·(계좌, signal_id) 유일성은 메모리 집합이 아니라 **DB 유일 제약이 최종 보장**한다. 작업 집합 밖의 중복이 커밋 시 유일 제약에 걸리면 도메인 예외로 바꿔 메모리 구현과 같은 경로로 처리한다.
- N(설정, 기본 7일)은 데모 주문 보관 7일(M3-00)과 동기화 재조회 범위보다 짧으면 안 된다. 설정 검증에서 확인.
- 이 방식 대신 저장소·호출부를 async로 바꾸는 편이 낫다고 판단되면 구현 전에 질문(별도 선행 작업 필요).

**잠금 안 대기 규칙**: 계좌 잠금 획득·작업 집합 적재·커밋 반영은 필수 DB I/O라 잠금 안에서 기다려도 된다. **거래소·AI·텔레그램 같은 외부 호출은 잠금 안에서 하지 않는다**(M1-06 주문 전송기는 SUBMITTED 커밋 후 잠금 밖에서 전송).

## 커밋 결과 불명 (PR #15 검토 1번)

연결이 끊기는 시점에 따라 결과가 다르다.

| 장애 시점 | 판정 | 처리 |
|---|---|---|
| COMMIT 요청 전(적재·조작·반영 중) | 롤백 | 예외를 올리고 끝. 호출자는 같은 입력으로 다시 시도 가능 |
| COMMIT 요청 후 응답 전 | **결과 불명** | 아래 절차. 결과 확인 전에는 새 ID 발급·재처리·거래소 전송 금지 |

결과 불명 절차:

1. 모든 트랜잭션은 반영 단계에서 고유한 `txn_id`를 `uow_commits` 테이블에 같은 트랜잭션으로 넣는다.
2. 결과 불명이면 `CommitOutcomeUnknown(txn_id)`를 올리고, 그 계좌의 신규 진입을 `ENTRY_PAUSED_SYSTEM(storage)`로 멈춘다(메모리 정지 + 알림, DB에 쓰지 않음).
3. 새 연결로 같은 계좌 잠금을 얻은 뒤 `uow_commits`에서 `txn_id`를 찾는다. 있으면 커밋됨(호출자는 커밋 성공으로 이어서 진행, 예: SUBMITTED 주문 전송), 없으면 롤백됨(같은 입력으로 다시 시도). 잠금을 얻은 뒤 조회하므로 진행 중인 커밋과 섞이지 않는다.
4. 재연결·조회가 설정 시간 안에 안 되면 정지를 유지하고 알림. 손절은 거래소에 있으므로 청산하지 않는다(P1).
5. `uow_commits`는 N일 지나면 정리한다.

- Oracle Transaction Guard는 thin 모드·Free 에디션 지원 조건을 확인한 뒤 별도로 결정한다. 위 표지 행 방식은 Transaction Guard 없이도 동작한다.
- 주문 전송기(M1-06)는 결과 불명인 SUBMITTED 커밋에 대해 위 3번 결과가 나오기 전에는 전송하지 않는다.

## 범위

### 만들 것

1. **마이그레이션** (`db/migrations/V001__init.sql` …)
   - 03 6.2절 테이블과 M1에서 생긴 저장소 전부: `orders`, `order_events`, `fills`(exec_id 유일), `books`(전략 장부), `decisions`((계좌, signal_id) 유일), `episodes`(전략·계약당 비종결 1개 — 함수 기반 유일 인덱스), `close_intents`, `protection_recoveries`, `portfolio_actions`(outbox, 작업 키 유일), `portfolio_symbol_state`(계약 정지·손절 조회 회차·조회 시각·마지막 체결 시각·정지 회차), `funding_applied`(펀딩 ID 유일), `protective_operations`((operation_id, phase) 유일), `risk_states`(정지 상태 JSON, 최고점 2개, 재설정 이력), `equity_snapshots`, `cash_flows`(flow_id 유일), `alerts`, `commands`, `config_versions`, `account_locks`, `uow_commits`, `schema_version`, 그리고 M3-04 경계 처리용 `boundary_inputs`·`boundary_outputs`·`boundary_checkpoints`·`position_snapshots`·`unallocated_funding`.
   - M1-04 연결 계약(outbox 인수·acknowledge, `ProtectionRecovery`, `recover_system_pause`의 조건)을 그대로 저장할 수 있어야 한다.
   - 금액·수량은 `NUMBER`(정밀도 지정 없음), 시각은 `TIMESTAMP WITH TIME ZONE`(UTC로 저장).
   - 마이그레이션 실행 명령: 버전 순서대로 한 번씩만 적용, 적용 기록. 시작 시 버전이 맞지 않으면 Trader가 시작을 거부(03 4.3절 2단계).
   - 계좌 잠금 행은 계좌 생성(첫 시작) 때 미리 만든다. 모든 쓰기 경로가 같은 잠금을 먼저 얻는다.
2. **`OracleUnitOfWork`** (`storage/oracle.py`) — 위 "연결 방식"과 "커밋 결과 불명" 그대로.
   - `python-oracledb` thin 모드, 비동기 API, 연결 풀.
   - 유일 제약 위반은 도메인 예외(`DuplicateExecId` 등)로 바꾼다.
   - Decimal ↔ NUMBER 변환 시 정밀도 손실이 없도록 `oracledb.defaults.fetch_decimals = True`.
3. **메모리 구현 정리**: 작업 집합 정의를 메모리 구현에도 적용(오래된 종결 행은 `all()`에서 빠짐), 결과 불명을 흉내 낼 수 있는 장애 주입 훅.
4. **읽기 전용 계정**: `RLG_READ`에 두 스키마 SELECT 권한만 주는 스크립트(대시보드·SQL Developer용).
5. **백업** (D74): Data Pump 내보내기 스크립트(PowerShell), `paths.<env>.backup_dir`를 Oracle `DIRECTORY` 객체로 등록하는 안내, 14일 지난 파일 삭제. 매일 실행은 M3-06 작업 스케줄러에 등록.

### 만들지 않을 것

- 대시보드(M5), 백테스트 저장(SQLite 그대로)

## 완료 조건

- [ ] **같은 계약 테스트를 두 구현에 돌린다**: M1-02·03·04의 저장소 관련 테스트를 메모리와 Oracle 양쪽에서 실행하는 매개변수화 테스트. Oracle 쪽은 환경 변수로 접속 정보가 있을 때만 실행(CI에서는 건너뜀).
- [ ] 작업 집합: N일보다 오래된 종결 주문이 `all()`에 없고, 종결되지 않은 에피소드가 참조하는 오래된 주문은 있음. N일보다 오래된 봉의 신호는 게이트가 거절. 작업 집합 밖 exec_id 중복은 커밋 시 도메인 예외.
- [ ] 동시성: 두 작업이 같은 계좌로 동시에 위험 게이트를 통과하려 할 때 하나만 통과(M1-03 동시 진입 테스트를 Oracle로). 잠금 획득 전에 읽은 상태를 쓰지 않음.
- [ ] **장애 지점 두 개를 따로 테스트**: (a) COMMIT 전 연결 끊김 → 아무것도 남지 않음, 같은 입력 재시도 가능. (b) COMMIT 후 응답 유실(DB에는 커밋됨) → `CommitOutcomeUnknown`, 진입 정지, 새 ID 발급·전송 없음, 재연결 후 `txn_id`로 "커밋됨" 판정 → 같은 주문으로 이어서 진행. (b′) 같은 상황에서 실제로는 롤백된 경우 → "롤백됨" 판정 → 재시도. (c) 재연결 실패 지속 → 정지 유지·알림.
- [ ] Decimal 왕복(`0.10000000`, 아주 작은 수량)과 UTC 시각 왕복.
- [ ] 마이그레이션 재실행이 아무것도 바꾸지 않음, 버전 불일치 시 시작 거부.
- [ ] (수동) 이 PC Oracle Free에서 `RLG_DEMO`로 전체 테스트 1회 실행 결과를 PR에 첨부.

## 검토 포인트

- 잠금 → 적재 순서가 지켜지는지, 교착 가능성(잠금은 계좌 하나만)
- 결과 불명에서 새 ID를 만들거나 전송하는 경로가 없는지
- 유일 제약과 예외 변환이 M1 로직의 기대와 맞는지
- 비밀번호가 접속 문자열·로그에 남지 않는지
