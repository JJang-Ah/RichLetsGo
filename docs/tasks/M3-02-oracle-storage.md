# M3-02 Oracle 저장소

- 상태: 명세 작성 완료, 검토 대기
- 구현: Codex / 검토: Claude Code
- 브랜치: `task/M3-02-oracle-storage`
- 근거: 03 5장(주문 상태·저널)·6장(스키마·테이블·백업), D29, D44, D46, D60, D61, D74, M1-02·03 저장소 인터페이스

## 목표

M1의 `UnitOfWork` 프로토콜을 Oracle Database Free로 구현해, 메모리 구현과 **같은 의미**(원자적 커밋, 유일성, 롤백)를 보장한다.

## 사용자 준비 (Q23)

- Oracle Database Free 설치(관리자 권한), SQL Developer 설치.
- `RLG_DEMO`·`RLG_LIVE`·`RLG_READ` 계정 생성 스크립트(`db/admin/create_users.sql`)는 이 작업에서 제공하고, 실행은 사용자가 SYS로 한다. 비밀번호는 자격 증명 관리자에만 저장.

## 범위

### 만들 것

1. **마이그레이션** (`db/migrations/V001__init.sql` …)
   - 03 6.2절 테이블과 M1에서 생긴 저장소 전부: `orders`, `order_events`, `fills`(exec_id 유일), `positions`(전략 장부), `decisions`((계좌, signal_id) 유일), `episodes`(전략·계약당 비종결 1개 — 함수 기반 유일 인덱스), `close_intents`, `protective_operations`((operation_id, phase) 유일), `risk_states`(정지 상태 JSON, 최고점 2개, 재설정 이력), `equity_snapshots`, `cash_flows`(flow_id 유일), `alerts`, `commands`, `config_versions`, `schema_version`.
   - 금액·수량은 `NUMBER`(정밀도 지정 없음), 시각은 `TIMESTAMP WITH TIME ZONE`(UTC로 저장).
   - 마이그레이션 실행 명령: 버전 순서대로 한 번씩만 적용, 적용 기록. 시작 시 버전이 맞지 않으면 Trader가 시작을 거부(03 4.3절 2단계).
2. **`OracleUnitOfWork`** (`storage/oracle.py`)
   - `python-oracledb` thin 모드, 비동기 API. 연결 풀.
   - 트랜잭션: `__aenter__`에서 시작, `commit`에서 커밋, 그 외 종료는 롤백(메모리 구현과 같음).
   - **계좌 단위 직렬화**: 위험 게이트(D46)와 체결 반영이 동시에 같은 계좌 상태를 읽지 않도록, 트랜잭션 시작 시 계좌 잠금 행(`account_locks`)을 `SELECT … FOR UPDATE`로 잡는다. 메모리 구현의 `asyncio.Lock`과 같은 효과.
   - 유일 제약 위반은 도메인 예외(`DuplicateExecId` 등)로 바꿔 M1 로직이 메모리 구현과 같은 경로를 타게 한다.
   - Decimal ↔ NUMBER 변환 시 정밀도 손실이 없도록 `oracledb.defaults.fetch_decimals = True`.
3. **읽기 전용 계정**: `RLG_READ`에 두 스키마 SELECT 권한만 주는 스크립트(대시보드·SQL Developer용).
4. **백업** (D74): Data Pump 내보내기 스크립트(PowerShell), `paths.<env>.backup_dir`를 Oracle `DIRECTORY` 객체로 등록하는 안내, 14일 지난 파일 삭제. 매일 실행은 M3-06 작업 스케줄러에 등록.

### 만들지 않을 것

- 대시보드(M5), 백테스트 저장(SQLite 그대로)

## 완료 조건

- [ ] **같은 계약 테스트를 두 구현에 돌린다**: M1-02·M1-03의 저장소 관련 테스트를 메모리와 Oracle 양쪽에서 실행하는 매개변수화 테스트. Oracle 쪽은 환경 변수로 접속 정보가 있을 때만 실행(CI에서는 건너뜀).
- [ ] 동시성: 두 작업이 같은 계좌로 동시에 위험 게이트를 통과하려 할 때 하나만 통과(M1-03 동시 진입 테스트를 Oracle로).
- [ ] 커밋 실패(연결 끊김 주입) 시 아무것도 남지 않음.
- [ ] Decimal 왕복(`0.10000000`, 아주 작은 수량)과 UTC 시각 왕복.
- [ ] 마이그레이션 재실행이 아무것도 바꾸지 않음, 버전 불일치 시 시작 거부.
- [ ] (수동) 이 PC Oracle Free에서 `RLG_DEMO`로 전체 테스트 1회 실행 결과를 PR에 첨부.

## 검토 포인트

- 계좌 잠금이 메모리 구현과 같은 직렬화를 보장하는지(잠금 순서, 교착 가능성)
- 유일 제약과 예외 변환이 M1 로직의 기대와 맞는지
- 비밀번호가 접속 문자열·로그에 남지 않는지
