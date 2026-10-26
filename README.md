# RichLetsGo — Bybit 선물 / 국내·미국 주식 자동매매

2026-10-19에 시작한 Codex + Claude Code 공동 개발 프로젝트입니다.

## 현재 상태

**M2-04 실제 데이터 검증은 네 조합 모두 FAIL이며 전략 재설계는 Claude·사용자가 진행합니다. Codex는 M3 인프라를 병행 구현합니다(D94).** 가상 거래소로 주문·복구 경로를 검증하며 실제 Bybit 어댑터와 운영 서비스는 M3에서 연결합니다. 초기 `trading_lab`은 새 도메인·위험·OMS 구현으로 대체했습니다(D39).

## 설계 문서

설계 문서가 구현의 기준입니다. 결정 번호(D1~)와 남은 질문(Q~)은 `00_DECISIONS.md`에서 관리합니다.

| 문서 | 내용 |
|---|---|
| [docs/design/00_DECISIONS.md](docs/design/00_DECISIONS.md) | 확정된 결정, 설계 원칙, 남은 질문 |
| [docs/design/01_STRATEGY_AND_AI.md](docs/design/01_STRATEGY_AND_AI.md) | 전략(S1 스윙 추세추종, S2 단기 평균회귀)과 AI 점수 |
| [docs/design/02_RISK_MANAGEMENT.md](docs/design/02_RISK_MANAGEMENT.md) | 한도 계층, 크기 계산, 정지 상태, 텔레그램 명령 |
| [docs/design/03_SYSTEM_ARCHITECTURE.md](docs/design/03_SYSTEM_ARCHITECTURE.md) | 프로세스, 모듈, 데이터 흐름, 주문 상태, Oracle 저장소 |
| [docs/design/04_VALIDATION_AND_OPERATIONS.md](docs/design/04_VALIDATION_AND_OPERATIONS.md) | 백테스트 → 데모 → 실거래 단계, 운영 체크리스트 |
| [docs/design/05_DEVELOPMENT_PROCESS.md](docs/design/05_DEVELOPMENT_PROCESS.md) | 역할 분담, PR 흐름, 마일스톤 |

## 범위 요약

- 첫 대상: Bybit USDT 무기한 BTCUSDT·ETHUSDT, 단방향, 격리 마진, 레버리지 3~5배 범위.
- 완전 자동매매, 사용자의 Windows PC에서 Windows 서비스로 실행, 주 저장소는 로컬 Oracle Database Free.
- AI 점수는 진입을 막거나 크기를 줄이기만 하며, 손절·한도는 규칙 엔진이 강제합니다.
- 한국투자증권 국내·미국 주식은 Bybit 실거래가 안정된 뒤 별도 설계로 추가합니다.

## 협업 방식

- 사용자: 결정, PR 병합. Claude Code: 설계·검토. Codex: 구현 (D37).
- 모든 변경은 브랜치 + PR + 다른 AI의 검토 후 병합합니다. `main`에 직접 커밋하지 않습니다 (D38).
- 두 AI 사이의 연락은 PR 코멘트와 [docs/HANDOFF.md](docs/HANDOFF.md)로 합니다.
- 공통 규칙: [docs/WORKING_RULES.md](docs/WORKING_RULES.md). `CLAUDE.md`와 `AGENTS.md`가 이 규칙을 가리킵니다.

## 다음 순서

1. Claude·사용자: 전략 재설계와 새 V1 검증
2. Codex: M3-01 설정·자격 증명·로그, M3-03 Bybit 오프라인 단위 테스트
3. Oracle 설치 뒤 M3-02, 새 전략 검증 통과 뒤 M3-07 데모 운영

## 개발 환경 (M0-01)

운영 기준은 **Python 3.14**, 최소 지원은 **3.12**입니다(D40). 3.12 미만은 지원하지 않습니다. 이 PC는 설치된 3.14를 그대로 사용합니다. 아래 명령은 PowerShell에서 프로젝트 루트를 기준으로 실행합니다.

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
.\.venv\Scripts\python.exe -m mypy
.\.venv\Scripts\python.exe -m pytest
```

다른 개발 환경에서도 Python 3.12 이상으로 `.venv`를 만든 뒤 같은 설치·검사 명령을 사용합니다. 가상환경 활성화 없이 해당 Python을 직접 실행하므로 PowerShell 실행 정책을 바꿀 필요가 없습니다.

- CI는 모든 PR과 `main` push에서 Windows / Python 3.12·3.14 검사를 각각 실행합니다.
- `src/richletsgo/domain`에 공통 타입과 검증 함수, `oms/`에 주문·체결·노출 정책, `storage/`에 저장소 계약과 메모리 구현이 있습니다. 실행 서비스는 아직 없습니다.
- `config.example.toml`은 비밀 값 없는 설정 예시입니다. 로컬 설정은 `config.local.toml`에 두며 Git에서 제외합니다. 실거래 위험 수치는 아직 미정이며, backtest.research_risk의 수치는 연구용입니다.
- 개발 도구는 테스트(`pytest`), 린트·포맷(`ruff`), 타입 검사(`mypy`)에만 사용합니다. M3-01부터 `keyring`을 Windows 자격 증명 관리자 접근에 사용합니다.

## 키 보관

키는 채팅이나 저장소에 붙여넣지 않습니다. Windows 자격 증명 관리자에 보관하며, 설정 파일·DB·로그에 넣지 않습니다. 실거래 Bybit 키는 출금 권한 없이 만듭니다.

## 참고 문서

- Bybit V5 API: https://bybit-exchange.github.io/docs/v5/intro
- Bybit 데모: https://bybit-exchange.github.io/docs/v5/demo
- 한국투자증권: https://github.com/koreainvestment/open-trading-api

## 도메인 모델 (M1-01)

- `richletsgo.domain`에서 `Qty`, `Price`, `InstrumentSpec`, 이벤트, 열거형, 시계와 ID 함수를 가져옵니다. 수량·가격 래퍼의 Decimal은 `.value`로 읽으며, int·float·문자열의 암묵적 변환은 허용하지 않습니다.
- `Qty(Decimal("0"))`는 빈 포지션을 나타냅니다. 주문 방향 변환과 체결은 0 수량을 거절합니다. `PositionSnapshot.avg_price`는 빈 포지션에서 `None`일 수 있습니다.
- `InstrumentSpec`은 전달받은 step/tick의 배수로 내림·올림합니다. 가격 내림 결과가 0이면 유효한 `Price`가 아니므로 호출자가 진입을 거절해야 합니다. 손절 방향 선택과 위험 검사는 M1-03에서 구현합니다.
- 이벤트의 미확인 orderLinkId·부모 ID는 `None`입니다. 어댑터는 거래소의 빈 ID를 `None`으로 변환합니다. `OrderEvent.raw`는 원본을 복사한 읽기 전용 매핑이며, 중첩 배열은 tuple로 보관합니다. 원문 데이터는 거래 계산의 입력 검증과 구분합니다.
- `make_order_link_id(..., unique_factory=...)`에는 재시작을 포함해 고유한 ASCII 문자열 생성기를 주입합니다(접미사 1~29자, 전체 최대 36자). `new_operation_id(uuid_factory=...)`에는 UUID 생성기를 주입합니다. 도메인 모듈이 실제 시계나 난수를 직접 호출하지 않습니다.
- `FixedClock`과 `ManualClock`에는 UTC aware 시각을 전달합니다. `OrderState.rank`는 순서만 나타내며, 종결 상태 예외를 포함한 실제 상태 전이는 M1-02의 책임입니다.

## 파일 경로 설정 원칙

운영 폴더는 코드에 고정하지 않고 config.local.toml의 paths.<environment>에서 지정합니다. 상대 경로는 **설정 파일이 있는 폴더** 기준이며, 외부 위치를 절대 경로로 설정할 수도 있습니다. config.example.toml에 환경별 예시가 있습니다. 백테스트 설정 로더는 M2-03에 있으며, 운영 설정 로더는 M3-01의 load_config입니다. M1-06 저널은 호출자가 전달한 폴더를 사용합니다.

## 주문 상태와 체결 (M1-02)

- `oms.models.apply_order_event`는 불변 주문 레코드의 상태 전이와 누적 체결 최댓값을 계산합니다. `oms.fills.ingest_order_event`가 원문 기록과 보류 체결 반영을 한 트랜잭션으로 처리합니다.
- `oms.fills.apply_execution`은 exec_id 중복 제거, 주문 체결 합계, 장부 훅을 함께 커밋합니다. 훅은 전달된 UoW의 저장소만 변경해야 합니다. 외부 객체 변경·네트워크 호출은 롤백할 수 없으므로 훅 안에서 수행하지 않습니다. 전체 전략 장부는 M1-04 범위입니다.
- `storage.memory.InMemoryDatabase(account_id, environment)`를 계좌별로 만들고 `unit_of_work()`를 사용합니다. 서비스 함수가 트랜잭션을 열고 닫으므로 이미 열린 UoW를 넘기지 않습니다. 저장소 직접 접근·노출 조회는 `async with` 안에서 수행하며, 명시적 커밋 없이 나가면 변경을 버립니다.
- 귀속 불명 체결은 저장 후 보류하고 해당 계약의 진입 정지 사유를 반환합니다. `lookup_confirmed=True`는 성공한 REST 주문 조회 결과에만 설정합니다. 조회 실패나 WS 통지만으로 외부 체결을 확정하지 않습니다.
- `oms.recovery.resolve_unknown`은 다음 행동만 반환합니다. 호출자는 최초 UNKNOWN 시각, 알림 발행 여부, 실제 마지막 취소 시도 시각(`last_cancel_at`)을 보존하고 조회·취소·알림을 수행합니다. 취소 접수만으로 종결하지 않으며, 수동 부재 확정은 `confirm_not_found`가 반환한 감사 기록을 호출자가 보존해야 합니다.
- `oms.exposure.exposure`에는 현재 평가 가격과 슬리피지 버퍼를 전달합니다. 보유·미반영·예약을 나누고, 손절·청산 주문은 체결 장부 반영 전까지 노출을 줄이지 않습니다. `unbooked_work`의 최초 미반영 시각은 호출자가 보존하며 반복 통지로 기한을 초기화하지 않습니다. `pending_work`의 조회·알림 목록은 현재 처리 대상이며 호출자가 실행·중복 알림 관리를 맡습니다.
- 런타임 의존성, 실제 시계·거래소·DB·파일 접근은 추가하지 않았습니다. 영속 저장 및 복구 작업 연결은 M1-06/M3에서 진행합니다.

- UNKNOWN 판단에는 `cancel_retry_after`(양수 간격)도 필수로 전달합니다. 실패한 취소는 간격 경과 후 재요청하고, ACCEPTED는 상태 이벤트를 기다립니다. 첫 시도는 last_cancel_at=None이며, 호출자가 실제 요청을 보낼 때 시각을 갱신합니다. 수동 점검 알림이 먼저 반환되면 다음 판단에서 재시도할 수 있습니다.
- 귀속 보류 체결은 `pending_qty`와 `pending_notional`로 별도 표시합니다. 명목 금액은 체결별 |수량|×체결가를 합산하고 계좌 합계에 포함합니다. 귀속을 알 수 없으므로 전략 필터에도 보수적으로 포함하지만 계약 필터는 적용합니다. 전략별 조회를 더해 계좌 합계를 만들지 말고 전략 필터 없는 조회를 사용합니다.

## 위험 엔진 (M1-03)

- `risk.limits.RiskLimits`: 02 7장 Q13의 모든 값을 필수로 받습니다. 기본값이 없고, 테스트 값은 운용 권고가 아닙니다.
- `risk.sizing.size_entry`: 손절가를 진입가에서 멀어지는 쪽으로 tick 정렬한 뒤, 허용 손실 ÷ (손절 거리 + 슬리피지 + 왕복 수수료)로 수량을 정합니다. 레버리지는 증거금 계산에만 씁니다. 강제청산 검사는 02 3.1절 **근사식**이며 M3에서 거래소 값으로 다시 확인합니다.
- `risk.equity.EquityTracker`: 지갑 잔고와 미실현 손익을 따로 받습니다(D72). 일일 손익 = (평가액 − 순입금액) − 거래일 기준값(D81). 입출금은 그 입출금이 반영된 스냅샷과 **함께** `observe`에 넘깁니다. 거래일 경계에서는 호출자가 펀딩 정산 뒤 `start_day`를 부릅니다(D76).
- `risk.pause.PauseState`: 정지 상태와 JSON 직렬화를 제공합니다. 정지는 신규 진입만 막고, 축소·청산 주문은 막지 않습니다.
- `risk.checks`: `evaluate_pauses`는 L4~L7에 해당하는 정지를 돌려줍니다(한도에 **닿으면** 정지). `check_limits`는 L2·L3·레버리지·정지를 검사하며, 전략 자기 보유를 0을 넘지 않게 줄이는 주문은 모든 검사에서 면제합니다.
- `risk.gate.RiskGate.admit`: 계좌 잠금 안의 한 트랜잭션에서 (계좌, signal_id) 결정 재사용 → 열린 에피소드 확인 → 크기·한도 검사 → 결정·주문(`CREATED`)·에피소드를 함께 커밋합니다(D46, D59, D60). 거래소·AI 호출은 받지 않습니다.
- `risk.guards`: 시세 지연 판단(`assess_market_data`)과 명령 권한 확인(`authorize_command`). `risk.resume.ResumeService`: 확인 단계를 거치는 `/resume`(D48).


## 위험 엔진 연결 계약 (M1-03)

- 활성 요청 전략의 손익·연속 손실 입력은 필수입니다. 배분 0인 전략은 입력 유무와 무관하게 정지합니다. UoW 잠금 획득 뒤 최신 입력으로 정지를 갱신하며, 계좌 조정 흐름은 대기 중 갱신된 전략 입력을 제공해야 합니다.
- 지정가 진입의 크기·손절 방향·강제청산·예약 기준가는 지정가입니다. 시장가만 진입 예상가를 사용합니다.
- `start_day(snapshot, cash_flows)`에는 경계 스냅샷에 이미 반영된 입출금을 함께 전달합니다. 최고점을 보정하고 중복 ID는 무시하지만, 새 지갑 기준에 포함된 금액을 새날 순입금에 다시 넣지 않습니다.
- 재개 확인은 pause_revision과 record_peak가 요청 당시와 같아야 한다. 평가액 변동은 |현재 평가액 - 요청 평가액| <= |요청 평가액| × resume_equity_tolerance_ratio이면 허용한다(상승·하락 대칭, 경계 포함). 이 비율은 RiskLimits의 필수 Decimal 설정이며 기본값 없이 0 이상 1 미만으로 받는다. 초과 시 새 확인을 요구한다. HALTED 재개 시 양수인 확인 시점 현재 평가액으로 위험 최고점을 설정하고 실제 적용값을 ResumeRecord.peak_reset.new_risk_peak에 기록한다. 프롬프트에는 허용 비율도 포함하여 호출자가 안내한다. 성공하면 기존 프롬프트를 모두 무효화한다.

## 과거 시장 데이터 수집 (M2-01)

백테스트용 Bybit 공개 데이터를 받습니다(API 키 없음). 설정 파일의 `[bybit_public] base_url`과 `paths.backtest.market_data_dir`만 읽고, 상대 경로는 설정 파일 위치 기준입니다.

```powershell
.\.venv\Scripts\python.exe -m richletsgo.marketdata.ingest --config config.local.toml --symbols BTCUSDT ETHUSDT --intervals D 60
```

- 기본 기간은 최근 3년(D70)이고 `--since`로 바꿀 수 있습니다. 다시 실행하면 빠진 구간만 받습니다.
- 1분봉은 `--intervals 1 --from <UTC> --to <UTC>`로 필요한 구간만 받습니다(`--to`는 미포함).
- 마감된 봉만 저장합니다. 빠진 봉·펀딩은 지어내지 않고 보고합니다. 저장된 값과 다른 값이 오면 덮어쓰지 않고 실패로 끝냅니다.
- 계약·주기 하나가 한 트랜잭션입니다. 응답 충돌이나 요청 실패가 있으면 그 계열은 아무것도 저장하지 않습니다. 다시 실행하면 처음부터 받습니다.
- 상품 규칙과 위험 한도 단계는 실행할 때마다 그 시각 값으로 저장합니다(과거 시점 값이 아님). Bybit linear의 증거금률은 비율입니다.

## 포트폴리오·보호 주문 (M1-04)

- `portfolio.signals`: 계좌 독립 신호와 공유 불변 AI 점수, `SignalRouter`의 신호 처리. AI 실패는 진입을 막고, 퇴장 신호는 AI·진입 정지와 무관하게 청산 의도를 남긴다.
- `portfolio.service`: OMS 체결 훅으로 평균가·실현 손익·수수료·펀딩·손절 귀속을 갱신한다. 장부 수량·주문·체결·에피소드·outbox는 계좌 UoW 안에서 함께 커밋된다.
- `portfolio.closing`: 순수 청산 상태 기계. 남은 진입 취소와 잔량 청산 → 다른 전략 보호 확인 → 자기 손절 취소 → 새 조회 검증 순서다. 재시도 횟수·간격은 `ClosePolicy` 필수 입력이다.
- 외부 작업은 `uow.portfolio.actions()`로 읽는다. 작업 키를 보존해 저널/전송을 인수한 뒤 acknowledge한다. 전송 결과는 이벤트로 반영하고, 전체 손절 조회 성공 후 `confirm_stop_snapshot`을 호출한다. 실패/부분 조회를 완료 조회로 넘기면 안 된다.
- 자세한 호출·복구 계약과 완료 조건은 [M1-04 명세](docs/tasks/M1-04-portfolio-and-protection.md)에 있다. 실제 전송·영속 복구는 M1-05/M1-06/M3에서 연결한다.

- 일반 보호는 체결 후 시작한 전체 손절 조회로 부족분을 확인한 뒤 요청한다. 복구 횟수·시각을 저장하고 `ClosePolicy` 한도·간격을 적용한다. 미확정 보호는 중복 생성하지 않고 시간 초과 시 계약 정지·알림·미보호 수량 축소를 요청한다.
- 계약 정지 해제는 전체 계좌 동기화 후 `recover_system_pause`로 검증한다. 포지션·손절 수량이 맞아야 하며, 미확정 주문/체결·미완료 청산 등이 없어야 한다. 수동 정지와 HALTED는 해제하지 않는다.


## 가상 거래소 (M1-05)

- `exchange.contracts.ExchangeGateway`는 비동기 주문·조회·개인 이벤트 계약이다. `OrderRequest.from_record`로 OMS 주문을 변환하며, 응답 불명은 `ExchangeError(ErrorCode.TIMEOUT, ...)`으로 반환한다.
- `exchange.simulated.SimulatedExchange`는 마감 봉을 `advance_bar`로 받아 다음 봉 시가 체결, 지정가 엄격 돌파, Partial 부착 손절, reduce-only, 수수료·펀딩을 재현한다. 시계·상품 규칙·수수료·슬리피지는 호출자가 주입한다.
- `FaultPlan`은 응답 유실·조회 지연/오류·중복/역순 이벤트·연결 끊김·IOC 부분 체결 등을 결정적으로 재현한다. 실제 거래소에는 연결하지 않는다.
- 펀딩 경계 처리, 손절 축소 가정, 합성 체결 시각, 스트림 재연결 등의 호출 계약은 [M1-05 명세](docs/tasks/M1-05-simulated-exchange.md)에 있다. 실제 전송·복구 조립은 M1-06/M3에서 연결한다.


## 복구·보호 저널 (M1-06)

- `oms.synchronizer.Synchronizer`에 계좌 UoW, 게이트웨이, PortfolioService, 저널, 시계, 공유 PauseState, 재시작에도 고유한 ID 생성기, SyncPolicy, 계약 목록을 주입합니다. 계좌별 조정자·저널 작성자는 하나입니다.
- 시작·재연결에 `await synchronize()`를 호출하고, False이면 진입 정지를 유지합니다. 정상 주문은 `submit(order)`, 이벤트·재시도 처리는 `tick()`, 종료는 `close()`입니다. 타이머와 재연결 자체는 M3 호출자가 담당합니다.
- `ProtectiveJournal`에는 설정 로더가 설정 파일 기준으로 해석한 Path를 전달합니다. 파일 구간·보관·손상 꼬리 격리 위치는 모두 그 폴더 아래입니다. 실제 시계·운영 폴더 기본값은 없습니다.
- `OrderSender`는 진입 SUBMITTED 커밋 뒤 전송합니다. 저장소 장애 시 이미 보유한 보호 주문 레코드는 같은 ID로 저널 전송할 수 있습니다. 새 정책 판단·장부 갱신은 저장소 복구 후 동기화가 맡습니다. Oracle 커밋 결과 불명 판정은 M3-02 계약을 따릅니다.
- D90 `amend_stop`은 trigger 또는 **부호 있는 전체 수량**을 받습니다. 수량 변경 미지원이면 새 보호를 먼저 발행하고 전체 조회로 전략별 보호를 검증한 뒤 기존 손절을 취소합니다. 교체 작업 ID·요청별 ID·재시도 간격과 한도를 보존합니다.
- [V0 39행 대응표](docs/tasks/M1-06-v0-matrix.md), [M1-06 명세와 연결 계약](docs/tasks/M1-06-sync-journal-v0.md)을 함께 확인합니다. 실제 Bybit Partial 손절 수량 변경 가능 여부는 M3-07 C10에서 확인합니다.


## 백테스트 실행기 (M2-03)

`richletsgo.backtest.runner.BacktestRun`은 M2-01 저장소를 검사한 뒤 실제 위험 게이트·포트폴리오·동기화기와 가상 거래소로 실행한다. API, 결과 규약과 제약은 [M2-03 명세](docs/tasks/M2-03-backtest-runner.md)를 참고한다. 연구 설정은 `load_research_config`로 읽으며 경로를 호출자에게 반환한다. 실행 시각과 코드 커밋도 호출자가 주입한다.

3년 합성 데이터 부하 측정(실제 시장 성과와 별개):

```powershell
.\.venv\Scripts\python.exe tools/benchmark_backtest.py --config config.example.toml --output <측정결과폴더>
```

## 구간 이동 검증과 보고서 (M2-04)

`config.example.toml`의 `[backtest.walkforward]`, `[backtest.grid]`, `[backtest.selection]`, 선택적인 `[backtest.pass_criteria]`를 사용합니다. 격자는 S1/S2 후보의 곱이며 조합당 최대 24개입니다. 예시는 각 후보군 2개, 조합당 4개이며 실제 운용 권고가 아닙니다. 기간·작업자 수·병렬 효율·계산 시간 상한에는 코드 기본값이 없습니다.

수집을 끝낸 다음 실행합니다. 예시 2023-10-01~2026-10-01에는 41일의 선행 워밍업이 필요하므로 아래 수집 시작일은 여유를 둡니다.

```powershell
python -m richletsgo.marketdata.ingest --config config.example.toml --symbols BTCUSDT ETHUSDT --intervals D 60 --since 2023-07-01T00:00:00Z --commit <수집 코드 커밋>
python -m richletsgo.backtest.walkforward --config config.example.toml --prepare-only
python -m richletsgo.backtest.walkforward --config config.example.toml
```

- 출력: 설정의 `backtest_results_dir/campaigns/<identity hash>/` 아래 `manifest.json`, 고정 설정, 읽기 전용 입력 스냅샷, `report.md`, `report-data.json`, 실행별 산출물. `--commit`은 Git을 쓸 수 없는 배포 환경에서 코드 식별자를 명시할 때 사용합니다.
- 원본 데이터와 스냅샷을 다시 해시하여 도중 수집·변경을 감지하면 새 결과 게시와 보고서 생성을 거절합니다. 같은 출력 디렉터리에는 OS 파일 잠금으로 작성자를 하나만 허용합니다. 잠금은 프로세스 종료 시 해제됩니다.
- 같은 설정·코드·입력으로 명령을 다시 실행하면 해시와 완료 상태를 검증한 산출물만 재사용합니다. 작업자 수·계산 예산 변경은 기존 캠페인에 적용할 수 있습니다. 성과에 영향을 주는 설정 또는 데이터가 바뀌면 별도 캠페인입니다.
- `SUMMARY`는 동일 계산 후 통계·거래 해시만 저장하며 전체 SQLite를 쓰지 않습니다. `FULL`은 기존 상세 SQLite도 저장하고 재사용 시 그 파일 해시도 검사합니다. 입력 캐시와 결과 SQLite는 Git에 올리지 않습니다.
- `--max-new-runs N`은 중단/재개 검증용으로 한 실행 단계의 신규 실행을 N개까지 게시한 뒤 중단합니다. 실패·INCOMPLETE·임시 파일은 재사용하지 않습니다.
- 계산량은 남은 학습 실행과 가능한 확인 실행의 상한으로 먼저 추정합니다. 확인 후보가 결정되면 실제 확인 실행만 재추정합니다. 최초 설정 횟수 이후 배치마다 실측으로 갱신하며 예산 초과 시 완료된 결과를 보존합니다.
- 충돌 기록에는 별도 해결 표시가 없으므로 충돌 수집 배치의 요청 범위를 보수적으로 미해결로 취급합니다. 그런 범위는 INVALID_DATA입니다. 원본 수집 기록을 삭제하여 결과를 통과시키지 않습니다.
- 보고서는 시간별 합성 평가액으로 낙폭을 계산하고 월말 표를 표시합니다. 보고서 본문 해시는 실행 메타(작업자 수·시간·재사용 횟수)를 제외합니다. 판정 기준 표가 없으면 UNSET, 무효/후보 없음 비율을 넘으면 INDETERMINATE입니다.

실제 3년 첫 검증 결과는 [M2-04 보고서](docs/reports/M2-04-2023-10-01-to-2026-10-01.md)에 있습니다. 네 조합 모두 V1 기준 미달이며, 설정을 결과에 맞춰 다시 조정하지 않았습니다.


## 서비스 설정·자격 증명·로그 (M3-01)

`richletsgo.config.load_config(Path(...))`는 불변 AppConfig와 비밀 없는 `public_dict()`/`config_hash`를 제공합니다. 경로는 설정 파일 기준으로 해석하며 환경 간 동일/상하위 폴더 중첩을 거부합니다. demo는 `[risk]` 필수 값을 사용하고 backtest만 `[backtest.research_risk]`를 사용합니다. 예시는 연구 수치를 demo에 명시한 것이며 live는 로더에서 거부합니다.

`config.example.toml`을 `config.local.toml`로 복사하고 환경, 사용자 ID, 발급 시각, 경로, Oracle DSN을 직접 맞춥니다. 현재 `strategy.*.source="manual"`은 개발용이며 새 전략 V1 통과 또는 M3-07 승인으로 취급하지 않습니다. PR #26에서 새 보고서 출처/운영 조건을 명세와 맞추도록 검토 의견을 남겼습니다.

서비스를 실행할 Windows 계정으로 다음 명령을 **직접** 실행합니다. 값은 숨김 입력이며 채팅이나 설정 파일에 넣지 않습니다.

```powershell
python -m richletsgo.keystore set richletsgo/bybit-demo --api
python -m richletsgo.keystore set richletsgo/telegram-bot
python -m richletsgo.keystore set richletsgo/oracle-demo
```

키 저장은 keyring의 Windows WinVault 백엔드만 사용합니다(평문·대체 백엔드로 자동 전환하지 않음). 저장 규약은 service=설정의 항목 이름, username=`credential`; API 값은 key/secret JSON 묶음입니다. 읽을 때는 `KeyStore(windows_backend()).api(item, config.api_created_at)`를 씁니다. 아직 실제 키를 등록하거나 읽지 않았으며 서비스 계정 접근 확인은 키 준비 후 수동 검증합니다.

`configure_logging(config.paths.log_dir, environment=config.environment, service="Trader", retention_days=config.log_retention_days, secrets=[credential.key, credential.secret])`는 UTC 일별 JSONL 회전과 기간 보관을 설정합니다. API 키·서명·토큰/비밀번호 필드, 등록된 비밀 문자열과 예외 본문을 마스킹합니다. 추가로 읽은 봇 토큰·DB 비밀번호도 secrets 목록에 넣어야 임의의 문장 안에서도 마스킹됩니다. 부모 로거로 전파하지 않습니다.

Oracle 접속은 M3-02이며 사용자 설치 뒤 시작합니다. M3-01 로더는 DB에 연결하지 않습니다. Data Pump DIRECTORY 등록 요구는 03 6.3절에 이미 명시돼 있습니다.


## Bybit 데모 어댑터 (M3-03)

`exchange.bybit`에 REST 서명/조회/주문과 개인·공개 WS를 구현했습니다. 생성 요청은 자동 재시도하지 않으며, 손절 정정은 D90의 부호 있는 전체 수량을 거래소 절대 수량으로 변환합니다. 연결 순서·오류/이벤트 매핑·서비스 수명과 남은 수동 검증은 [매핑 문서](src/richletsgo/exchange/bybit/MAPPING.md)를 참고합니다.

현재 검증은 가짜 REST/WS 응답을 사용하는 오프라인 테스트까지입니다. 실제 키나 주문을 사용하지 않았으며, 서비스 조립·Oracle·새 전략 V1 통과와 M3-07은 별도 단계입니다. M3-03 PR은 M3-01 PR 위에 쌓아 설정 변경과 어댑터 변경을 따로 검토할 수 있게 했습니다.
