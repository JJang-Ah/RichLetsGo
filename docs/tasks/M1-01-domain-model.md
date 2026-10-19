# M1-01 도메인 모델

- 상태: 명세 작성 완료, 사용자 확인 대기
- 구현: Codex / 검토: Claude Code
- 브랜치: `task/M1-01-domain-model`
- 근거: 03 4.1절(D53 수량 부호), 03 5장(orderLinkId 형식), 01 1.4절, REVIEW.md 발견 3·4·6·7, M0 검토 제안 1

## 목표

이후 모든 M1 작업이 같은 타입을 쓰도록 값 객체, 열거형, 이벤트, 상품 규칙, ID 생성, 시계 인터페이스를 만든다. 로직은 최소한으로 둔다.

## 범위

### 만들 것 (`src/richletsgo/domain/` 신설)

1. **열거형**
   - `Symbol`: `BTCUSDT`, `ETHUSDT` (D8). 문자열을 받을 때는 앞뒤 공백·대소문자를 정규화하지 않고 **정확히 일치할 때만** 허용한다(REVIEW 발견 3: 정규화 대신 거절).
   - `StrategyId`: `S1`, `S2`
   - `Environment`: `backtest`, `demo`, `live`
   - `OrderPurpose`: `ENTRY`(E), `CLOSE`(X), `PROTECT`(P), `ATTACHED_SL`(거래소가 만든 부착 손절)
   - `OrderState`: `CREATED`, `SUBMITTED`, `UNKNOWN`, `OPEN`, `PARTIALLY_FILLED`, `FILLED`, `CANCELLED`, `REJECTED`, `EXPIRED`, `NOT_FOUND`, `ABORTED`. 순서(rank)와 종결 여부를 열거형에서 제공한다(03 5.1절).
   - `Side`: `BUY`, `SELL` — **어댑터 경계에서만** 쓴다. 내부 로직은 부호 있는 수량을 쓴다.

2. **수량·가격** (D53)
   - `Qty`: 부호 있는 `Decimal` 래퍼 또는 `NewType`. 롱 +, 숏 −. 유한값만 허용.
   - `Price`: 양수 유한 `Decimal`.
   - 도우미: `side_and_abs(qty) -> (Side, Decimal)`, `signed(side, abs_qty) -> Qty`. 0 수량은 `side_and_abs`에서 거절.
   - `float`, `int`가 아닌 `Decimal`이 아닌 값, NaN, Infinity는 거절.

3. **상품 규칙** `InstrumentSpec`
   - 필드: `symbol`, `qty_step`, `min_qty`, `tick_size`, `max_leverage`(거래소 값).
   - `round_qty_down(abs_qty)`: qty_step 단위로 내림. 결과가 `min_qty` 미만이면 0을 돌려준다(최소 수량으로 올리지 않는다, 02 3장).
   - `round_price_down(price)`, `round_price_up(price)`: tick_size 단위 정렬.
   - 손절가 정렬 방향 규칙은 M1-03에서 쓴다: **진입가에서 멀어지는 쪽**(롱 손절은 내림, 숏 손절은 올림)으로 정렬하고, 크기 계산과 강제청산 여유 검사(P5)는 **정렬된 손절가**로 다시 한다. 그래서 정렬 때문에 실제 위험이 허용 손실을 넘지 않는다.
   - 값은 생성자로 주입한다(테스트용 고정값). 거래소에서 읽는 것은 M3.

4. **이벤트** (불변 dataclass, `frozen=True, slots=True`)
   - `ExecutionEvent`: `exec_id`, `order_link_id`, `exchange_order_id`, `symbol`, `qty`(부호 있음), `price`, `fee`, `exec_time`, `is_maker`
   - `OrderEvent`: `order_link_id`, `exchange_order_id`, `parent_order_link_id`(부착 손절이면 값 있음), `state`, `exchange_cum_exec_qty`(절댓값), `event_time`, `raw`(원문 dict, 기록용)
   - `PositionSnapshot`: `symbol`, `qty`(부호), `avg_price`, `snapshot_time`

5. **ID 생성**
   - `make_order_link_id(env, strategy, purpose) -> str`: 형식 `<env 1자>-<S1|S2>-<E|X|P>-<고유값>`, **36자 이하**. 고유값은 주입한 생성기로 만든다(테스트에서 결정적).
   - `parse_order_link_id(s)`: 위 형식을 다시 나눈다. 형식이 아니면 `None`(외부 주문으로 판단할 때 사용).
   - `new_operation_id() -> str`: 저널 작업 ID (D55). 생성기 주입.

6. **시계**
   - `Clock` 프로토콜: `now() -> datetime`(UTC aware).
   - `FixedClock`/`ManualClock`(테스트용, 시간 이동 가능).
   - `trading_day_start(ts) -> datetime`: UTC 00:00 기준 거래일 시작 (02 1장).

7. **패키징 보완** (M0 검토 제안 1)
   - `pyproject.toml`의 `[tool.setuptools.packages.find] include`를 `["richletsgo*"]`로 바꾼다.

### 만들지 않을 것

- 주문 상태 전이 로직 (M1-02), 위험 검사 (M1-03), 거래소 호출 (M1-05·M3)
- 설정 파일 로딩 (M3)

## 완료 조건

- [ ] `float`·NaN·Infinity·비 UTC 시각을 넣으면 거절되는 테스트
- [ ] `Symbol(" btcusdt\n")` 같은 입력이 거절되는 테스트 (REVIEW 발견 3)
- [ ] `round_qty_down`: 0.0000001 BTC 같은 단위 미만 수량 → 0 (REVIEW 발견 4), 최소 수량 미만 → 0
- [ ] `side_and_abs`/`signed` 왕복, 0 거절
- [ ] orderLinkId 36자 이하, 왕복 파싱, 다른 형식은 `None`
- [ ] `OrderState` rank 순서와 종결 여부가 03 5.1절과 일치
- [ ] 거래일 경계(UTC 23:59:59 → 00:00:00) 테스트
- [ ] 패키징 include 변경 후 스모크 테스트 통과
- [ ] ruff·mypy strict·pytest 통과 (3.12·3.14)

## 검토 포인트

- 내부 로직에 `Side`가 새어 들어가지 않는지 (부호 있는 수량만)
- 가격 정렬 함수가 내림·올림을 명확히 구분하는지
- 이벤트에 거래소 원문이 남는지(나중 대조용)
