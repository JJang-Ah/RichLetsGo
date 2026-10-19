# M1 핵심 로직 — 작업 개요

- 근거: [05_DEVELOPMENT_PROCESS.md](../design/05_DEVELOPMENT_PROCESS.md) 3장 M1, [04_VALIDATION_AND_OPERATIONS.md](../design/04_VALIDATION_AND_OPERATIONS.md) 2장(V0)
- 선행 조건: M0-01 병합 (PR #3)
- 구현: Codex / 검토: Claude Code (D37)

## 목표

외부 연결(네트워크, DB, 실제 시계) 없이 동작하는 매매 핵심 로직을 만들고, 04 2장의 V0 장애 시나리오를 모두 테스트로 고정한다. M1이 끝나면 같은 코드에 M3에서 Bybit·Oracle 어댑터만 붙이면 된다.

## 작업 순서와 의존 관계

| 작업 | 내용 | 의존 |
|---|---|---|
| [M1-01](M1-01-domain-model.md) | 도메인 모델: 수량·가격·부호, 주문·체결 이벤트, 상품 규칙, ID 생성, 시계 | M0 |
| [M1-02](M1-02-order-state-and-fills.md) | 주문 상태 기계, 체결 반영, 노출(보유·미반영·예약), UNKNOWN 해소 판단, 저장소 인터페이스 | M1-01 |
| [M1-03](M1-03-risk-engine.md) | 위험 엔진: 크기 계산, L1~L7, 위험 게이트(검사+예약), 정지 상태, HALTED 재개 | M1-02 |
| [M1-04](M1-04-portfolio-and-protection.md) | 신호·결정 멱등, 승인 목표, 전략 장부, 손절 귀속, 손절 유지 청산, 외부 감소 | M1-03 |
| [M1-05](M1-05-simulated-exchange.md) | ExchangeGateway 인터페이스와 가상 거래소: 체결 규칙, Partial 손절, 장애 주입 | M1-01 (M1-02~04와 병행 가능) |
| [M1-06](M1-06-sync-journal-v0.md) | 시작·재연결 동기화, 로컬 저널, V0 시나리오 통합 테스트, `trading_lab` 삭제 | M1-02~05 |

- 작업 하나 = 브랜치 하나 = PR 하나 (`task/M1-0x-...`).
- M1-05는 M1-01만 있으면 시작할 수 있다. 같은 파일을 동시에 고치지 않도록 M1-05는 `exchange/` 아래만 만든다.

## M1 공통 규칙

- **외부 연결 금지**: 네트워크, 파일 DB, 실제 시계(`time.time`, `datetime.now`)를 직접 부르지 않는다. 시계·저장소·거래소는 인터페이스로 주입한다. 예외: M1-06의 로컬 저널은 파일을 쓰지만 디렉터리를 주입받고 테스트는 임시 디렉터리를 쓴다.
- **런타임 의존성 추가 없음.** 비동기 테스트는 표준 `asyncio.run` 또는 `unittest.IsolatedAsyncioTestCase`로 한다. 개발 의존성을 꼭 추가해야 하면 PR 설명에 이유를 쓴다.
- 금액·가격·수량은 `Decimal`. `float`를 받으면 거절한다(TypeError).
- 시각은 timezone 정보가 있는 UTC `datetime`만 받는다.
- 모든 공개 함수·클래스에 타입 힌트(mypy strict 통과).
- 설계에 없는 동작이 필요하면 구현 전에 PR이나 HANDOFF에 질문으로 남긴다(설계 문서를 먼저 고친다).
- 각 PR은 해당 명세의 "완료 조건" 체크리스트를 PR 설명에 붙이고 결과를 표시한다.

## M1 완료 조건 (마일스톤 전체)

- 04 2장 V0 표의 모든 행이 테스트 이름으로 추적된다(M1-06에서 표 ↔ 테스트 대응표 작성).
- CI(3.12·3.14) 통과.
- `trading_lab/`, `tests/test_core.py`, pytest `pythonpath = ["."]` 삭제 (D39).
