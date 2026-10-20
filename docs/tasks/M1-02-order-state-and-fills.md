# M1-02 주문 상태와 체결 반영

- 상태: 명세 작성 완료, 사용자 확인 대기
- 구현: Codex / 검토: Claude Code
- 브랜치: `task/M1-02-order-state-and-fills`
- 근거: 03 5.1~5.3절 (D44, D54, D61), 02 2.1절 (D52, D57), 03 6.2절 테이블 초안

## 목표

주문 상태 전이, 체결 반영, 주문별 노출 계산, UNKNOWN 해소 판단을 순수 로직으로 만든다. 저장은 인터페이스 뒤의 메모리 구현으로 하고, "한 트랜잭션" 의미를 메모리 구현에서도 지킨다.

## 범위

### 만들 것

1. **주문 레코드** `OrderRecord` (`src/richletsgo/oms/`)
   - 필드: `order_link_id`, `exchange_order_id`, `parent_order_link_id`, `strategy`, `purpose`, `symbol`, `qty`(부호 있는 주문 수량), `limit_price`(없으면 시장가), `reference_price`, `state`, `booked_qty`(장부 반영 체결 합계, 부호 있음), `exchange_cum_exec_qty`(거래소 누적, 절댓값의 최댓값), `created_at`, `updated_at`.
   - 파생 값 (02 2.1절, D52) — **`purpose == ENTRY`인 주문만** 계산하고, 그 밖의 주문(`CLOSE`, `PROTECT`, `ATTACHED_SL`)은 항상 0이다 (D57). 손절·청산 주문은 체결되어 장부에 반영되기 전까지 노출을 줄이지 않는다.
     - `unbooked_qty` = sign(qty) × max(0, exchange_cum_exec_qty − |booked_qty|)
     - `reserved_qty` = 종결 전(`UNKNOWN` 포함)이면 sign(qty) × (|qty| − max(exchange_cum_exec_qty, |booked_qty|)), 종결이면 0
     - 노출 계산에 쓰는 보수적 가격: `limit_price` 또는 `reference_price × (1 + slippage_buffer)` (방향에 맞게)

2. **상태 전이** `apply_order_event(record, event) -> (record, Transition)`
   - 03 5.1절 규칙 그대로:
     - 앞으로만 간다. 중간 단계 건너뛰기 허용(`SUBMITTED` → `FILLED`).
     - 현재보다 앞 상태 이벤트는 상태를 바꾸지 않고 `Transition.IGNORED_STALE`로 기록.
     - 같은 상태 반복(예: `Filled` 두 번)은 `Transition.DUPLICATE`.
     - 종결 상태 불변. 예외 둘: `CANCELLED` 이후 체결은 반영하되 상태 유지, **사용자 수동 확정 `NOT_FOUND`** 이후 이벤트는 실제 상태로 정정(`Transition.CORRECTED_FROM_NOT_FOUND`, 알림 대상).
   - `exchange_cum_exec_qty`는 이벤트 값과 기존 값 중 큰 값으로만 갱신.
   - 모든 이벤트(무시한 것 포함)를 `order_events`에 원문과 함께 추가하는 것은 저장소 계층 책임 — 전이 함수는 기록할 내용을 돌려준다.

3. **체결 반영** `apply_execution(uow, event)`
   - `exec_id` 중복이면 아무것도 바꾸지 않고 `DUPLICATE` 반환.
   - 하나의 작업 단위(unit of work) 안에서: 체결 저장, 해당 주문 `booked_qty` 증가, (M1-04가 등록할) 장부 갱신 훅 호출. 중간에 예외가 나면 전부 되돌린다.
   - 주문 상태와 무관하게 항상 반영한다(종결 상태여도).
   - **귀속 보류 (D61)**: 체결의 주문을 아직 알 수 없으면(orderLinkId가 없고, 같은 거래소 주문 ID의 주문 레코드나 부착 손절의 부모 연결도 아직 없음) 체결은 저장하되(exec_id 중복 제거) 장부 훅을 부르지 않고 `PENDING_ATTRIBUTION`으로 표시한다.
     - 같은 거래소 주문 ID의 `OrderEvent`가 들어오면(부착 손절이면 `parent_order_link_id` 포함) 보류 체결의 귀속을 정하고, **한 작업 단위 안에서** 장부 훅 호출 + 보류 해제를 한다.
     - 보류가 정해진 시간 안에 해소되지 않으면 "거래소 주문 ID로 주문 조회" 요청을 낸다(조회는 호출자/M1-06이 수행하고 결과를 다시 넣는다).
     - **외부 감소 확정**은 조회가 성공했고 그 주문의 orderLinkId와 부모 orderLinkId가 모두 우리 형식이 아닐 때만(`parse_order_link_id`가 `None`). 그때 `EXTERNAL`로 M1-04의 외부 감소 처리로 넘긴다. 조회 실패 → 보류 유지, 기한 후 알림.
     - 보류 중인 체결이 있는 계약은 신규 진입 정지 사유가 된다(M1-03의 계약 단위 `ENTRY_PAUSED_SYSTEM`).

4. **UNKNOWN 해소 판단** (D54) — 순수 함수
   - 입력: 주문 레코드, 조회 결과 목록(각 조회의 성공/오류, 찾은 상태), 취소 시도 결과.
   - 출력: 다음 행동(`KEEP_UNKNOWN`, `MOVE_TO(state)`, `SEND_CANCEL`(진입 주문만), `ALERT_MANUAL_CHECK`).
   - 조회가 비어 있어도 **절대 `NOT_FOUND`로 가지 않는다.** `NOT_FOUND`는 수동 확정 함수 `confirm_not_found(record, actor, at)`로만 간다(텔레그램 명령은 M3).
   - 기한(설정값, 예: 10분) 경과 → `ALERT_MANUAL_CHECK`를 한 번 낸다. 예약·정지는 유지.
   - 취소 결과 "주문 없음/취소하기 늦음" → `KEEP_UNKNOWN`.

5. **저장소 인터페이스** (`src/richletsgo/storage/`)
   - `OrderRepository`, `FillRepository`, `OrderEventLog`와 이를 묶는 `UnitOfWork` 프로토콜(비동기 컨텍스트 매니저, commit/rollback).
   - `InMemoryUnitOfWork`: 테스트용. 롤백 시 모든 변경을 되돌린다. `fills.exec_id` 유일성 보장.
   - Oracle 구현은 M3. 인터페이스는 Oracle의 트랜잭션 의미(원자적 커밋)를 가정한다.

6. **노출 합계** `exposure(uow, symbol=None) -> ExposureView`
   - 계약별로 보유(장부), 미반영 체결, 예약을 따로 돌려준다(부호 있는 수량과 보수적 명목 금액). 위험 게이트(M1-03)가 이 값을 쓴다.

### 만들지 않을 것

- 전략 장부 배분·손절 귀속 (M1-04) — 여기서는 훅 인터페이스만
- 거래소 호출 (M1-05·M3), 저널 (M1-06)

## 완료 조건 (04 2장 V0 중 이 작업 범위)

- [ ] 롱 +1 보유에 미체결 `ATTACHED_SL` −1, 미체결 `CLOSE` −1이 있어도 계약 노출은 +1 그대로. 숏도 같은 방식 (D57)
- [ ] `CLOSE` 주문의 미반영 체결이 있어도 장부 반영 전까지 노출은 줄지 않음
- [ ] 두 전략 보유 중 S1 부착 손절의 execution이 부모 연결 `OrderEvent`보다 먼저 도착 → 보류 → 주문 이벤트 도착 후 S1 장부에서만 감소 (S2 불변). 같은 execution이 REST로 다시 와도 한 번만 반영 (D61)
- [ ] 보류 체결 → 주문 조회 성공, 우리 형식 아님 → `EXTERNAL` / 조회 실패 → 보류 유지, 기한 후 알림
- [ ] 보류 해제(장부 반영) 중 예외 → 보류 상태로 롤백

- [ ] `SUBMITTED`에서 `FILLED` 직접 이동 (WS 선도착)
- [ ] `Filled` 두 번 → 두 번째 `DUPLICATE`, 상태 유지
- [ ] `CANCELLED` 확정 뒤 체결 도착 → 체결 반영, 상태 `CANCELLED` 유지, `booked_qty` 증가
- [ ] 같은 `exec_id`가 두 번(WS·REST) → 장부 한 번만 반영
- [ ] 체결 반영 중 훅에서 예외 → 체결·주문·장부 모두 롤백
- [ ] 주문 1 중 0.4 체결, `CANCELLED` 통지 선도착(execution 없음) → `unbooked` 0.4, `reserved` 0 → execution 도착 후 `booked` 0.4, `unbooked` 0. `FILLED` 선도착도 같은 방식 (D52)
- [ ] `UNKNOWN` + 조회 3종 성공·빈 결과 여러 번 → 계속 `KEEP_UNKNOWN`, 기한 후 `ALERT_MANUAL_CHECK` 한 번, 예약 유지
- [ ] `UNKNOWN` → 조회 결과 `PARTIALLY_FILLED` → 그 상태로 이동, 예약은 남은 수량
- [ ] `UNKNOWN` 진입 주문 → `SEND_CANCEL` → 취소 성공 → 이어지는 `CANCELLED`로 종결 / "주문 없음" → `KEEP_UNKNOWN`
- [ ] 보호 주문(`PROTECT`, `ATTACHED_SL`)은 `SEND_CANCEL` 대상이 아님
- [ ] 수동 `NOT_FOUND` 뒤 이벤트 도착 → 실제 상태로 정정, `CORRECTED_FROM_NOT_FOUND`
- [ ] 숏 주문(음수 수량)에서도 위 노출 계산의 부호가 맞음

## 검토 포인트

- 노출 3분류가 겹치거나 빠지는 경우가 없는지(합계 = 주문 수량의 체결 가능 최대치)
- 종결 상태 예외가 정확히 두 개뿐인지
- UNKNOWN 판단 함수에 시간 경과만으로 해제하는 경로가 없는지
- 메모리 UoW의 롤백이 실제로 모든 저장소에 적용되는지
