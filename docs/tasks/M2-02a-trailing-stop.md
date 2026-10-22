# M2-02a 추적 손절 연결

- 상태: 구현 완료, Claude Code 검토 대기
- 구현: Codex / 검토: Claude Code
- 브랜치: `task/M2-02a-trailing-stop`
- 순서: 이 작업을 **M2-02(지표·전략)보다 먼저** 별도 PR로 한다(PR #20 Codex 제안). 돈이 오가는 경로라 전략 코드와 따로 검토한다.
- 근거: 01 1.4절 "추적 손절(S1): 손절 trigger를 amend, 실패하면 기존 손절을 유지하고 다음 판단 때 다시 시도", D41·D42, D68, D90(재생 규칙), M1-04·M1-06 코드

## 목표

S1이 계산한 "원하는 손절가"를 거래소 손절 주문까지 반영한다. 병합된 코드에는 이 경로가 없다. M1-04 `ActionKind`에 trigger 수정 작업이 없고, M1-06 `OrderSender.amend(trigger=…)`를 부르는 곳이 없다.

**지켜야 할 성질**: 손절은 한 에피소드 안에서 **불리한 쪽으로 절대 움직이지 않는다**. 실패하면 기존 손절이 그대로 남는다. 목표에 못 미친 손절은 언젠가 다시 맞춘다.

## 용어

- **목표**(`book.stop_price`): 그 전략·계약에 대해 지금 유지하려는 손절가. 장부에 저장한다.
- **실제 trigger**: 각 손절 주문(`ATTACHED_SL`·`PROTECT`)이 거래소에서 확인된 trigger 가격(주문 이벤트·조회로 반영된 `OrderRecord.trigger_price`).
- **유리하다**: 롱이면 더 높은 값, 숏이면 더 낮은 값.

## 범위

### 만들 것

1. **목표 갱신** `PortfolioService.trail_stop(uow, strategy, symbol: Symbol, spec: InstrumentSpec, desired: Price)` (PR #20 검토 4번·재검토 2번)
   - 대상 계약 `symbol`과 상품 규칙 `spec`을 **따로** 받는다. 장부는 `symbol`로 찾고, `spec.symbol != symbol`이면 **아무것도 바꾸기 전에** 거절한다(장부·outbox·시도 기록 모두 그대로). 호출자(M2-03 백테스트 실행기, M3-04 Trader)도 같은 입력 계약을 쓴다.
   - tick 정렬: 롱 손절은 `spec.round_price_down`, 숏은 `round_price_up`(보수적 방향).
   - 열린 에피소드가 있고, 그 계약에 끝나지 않은 청산 의도가 없을 때만 동작한다.
   - 정렬한 값이 현재 목표보다 **유리할 때만** 목표를 바꾼다. 같거나 불리하면 목표는 그대로다.
   - 이 함수는 **목표만** 바꾼다. 주문 작업은 2번이 만든다.

2. **주문별 맞추기** (PR #20 검토 1번) — `reconcile` 안에서 매번 실행
   - 그 전략의 **확인된** 손절(OPEN·PARTIALLY_FILLED)마다 실제 trigger와 목표를 비교한다. 실제가 목표보다 불리하면 `AMEND_STOP(order_key, trigger=목표)` 작업을 낸다. **목표가 바뀌지 않았어도** 실제가 못 미치면 낸다(거절된 뒤 같은 목표 재시도, 늦게 OPEN이 확인된 손절, 늦은 진입 체결로 새로 붙은 손절 모두 이 규칙으로 맞춰진다).
   - 미확인 손절(CREATED·SUBMITTED·UNKNOWN)에는 내지 않는다. 확인된 뒤 다음 `reconcile`에서 맞춘다.
   - 청산 의도가 진행 중이면 내지 않는다(청산 상태 기계가 손절을 관리).
   - **진행 중인 amend가 있으면 새로 내지 않는다.** 같은 주문에 대해 결과가 확정되지 않은 amend(저널 `requested`만 있음, 또는 결과가 TIMEOUT)가 있으면, 먼저 주문 조회로 판정한다(D90 재생 규칙: 적용됨 / 미적용 / 대체됨). 판정 뒤 실제 trigger가 여전히 목표에 못 미칠 때만 다음 시도를 낸다.
   - **시도 기록** `TrailRecovery(order_key, target, attempts, last_attempt_at, in_flight, discards)` — 계좌 UoW에 저장해 재시작해도 유지한다 (PR #20 재검토 1번).
     - `attempts`는 **거래소에 실제로 보낸 요청 수**다. 동기화기가 전송을 인수하는 순간(저널 `requested` 기록과 같은 시점) 1 올리고, `last_attempt_at`·`in_flight = 그 요청의 operation_id`를 남긴다. 거절·TIMEOUT·Accepted 모두 한 번으로 센다. 전송 직전 확인(3번)으로 보내지 않고 버린 작업은 세지 않는다.
     - 작업 키: `amend:{order_key}:{target}:{attempts}:{discards}`. 키에 들어가는 `attempts`는 **다음에 보낼 시도 번호**라서, 한 번 보낸 뒤에는 키가 반드시 바뀐다. 그래서 `PortfolioRepository.emit`이 acknowledge된 키를 영구 보존해도, `OrderSender._claimed`가 같은 키를 막아도 재시도가 막히지 않는다. 같은 판단을 두 번 처리하면(보내기 전) 같은 키라 작업이 하나다.
     - PR #21 검토 반영: 전송 전 폐기는 `discards`만 올리고 acknowledge와 같은 UoW에서 저장한다. 실제 시도 횟수·마지막 전송 시각은 바꾸지 않는다. 일시적인 다른 전략 청산이 끝나면 같은 목표·같은 시도 번호라도 새 폐기 번호로 작업을 만들 수 있다. 폐기 번호도 재시작 시 복원한다.
     - `ClosePolicy.retry_after` 간격, `max_protect_attempts` 횟수. 한도를 넘으면 그 계약 신규 진입 정지 + 알림. 기존 손절은 그대로 둔다. 목표가 더 유리하게 바뀌면 새 목표에 대한 시도 기록을 새로 시작한다.
   - **진행 중(`in_flight`)의 끝** — 다음 중 하나가 확인돼야 끝난다. 그 전에는 새 작업을 내지 않는다.
     - 주문 이벤트·조회에서 실제 trigger가 요청 값이 됨 → **적용됨**.
     - 거래소가 거절(Rejected·UNSUPPORTED) → **미적용**.
     - TIMEOUT 또는 **Accepted 뒤 이벤트가 오지 않음** → 조회로 판정한다(D90 재생 규칙: 요청 값 = 적용됨, 이전 값 = 미적용, 그 밖·종결 = 대체됨). Accepted는 접수일 뿐 적용이 아니다(Bybit amend는 비동기, M1-06). 조회는 `retry_after`가 지난 뒤 하고, 그때도 이전 값이면 미적용으로 본다.
     - 끝나면 `in_flight`를 비우고, 실제 trigger가 여전히 목표에 못 미치면 다음 번호로 새 작업을 낸다.

3. **전송 직전 확인** (M1-06 동기화기, PR #20 검토 1번)
   - `AMEND_STOP`을 실행하기 직전에 주문·장부를 다시 읽는다. 다음 중 하나면 보내지 않고 acknowledge만 한다(다음 `reconcile`이 현재 상태로 다시 판단).
     - 주문이 종결됐다.
     - 청산 의도가 생겼다.
     - 실제 trigger가 이미 작업 trigger보다 같거나 유리하다.
     - 작업 trigger가 현재 목표보다 불리하다(오래 기다린 작업).
   - 이 확인으로 **개선된 손절을 과거 값으로 낮추는 일이 없다.**
   - 전송: `sender.amend(order, trigger=…)`(저널 대상, D45·D65). 전송을 인수할 때 2번 규칙대로 시도 번호를 올리고 `in_flight`를 남긴다. 결과(거절·TIMEOUT·Accepted)는 2번 "진행 중의 끝" 규칙으로 판정하고, 그동안 기존 손절을 유지한다.

4. **trigger만 바꾸는 amend의 미지원 응답** (PR #20 검토 2번)
   - 현재 `Synchronizer._resume_replacements`는 action_key가 있는 **모든** amend `UNSUPPORTED` 저널을 D90 수량 교체로 보고 `payload["after"]["qty"]`를 읽는다. trigger만 바꾼 amend는 이 값이 `None`이라 `Decimal(None)`에서 멈춘다.
   - 수정: 요청 `fields`에 `qty`가 있는 저널만 D90 교체 대상으로 고른다. trigger만 바꾼 `UNSUPPORTED`는 거절과 같게 처리한다(미적용으로 판정해 `in_flight`를 끝냄, 기존 손절 유지, 한도 후 정지·알림). 이 메서드 수정도 이 작업 범위에 넣는다.

5. **늦은 진입 체결이 목표를 되돌리지 않게** (PR #20 검토 3번)
   - 현재 `PortfolioService.book_execution`(service.py 139~140)은 진입 체결마다 `book.stop_price = order.attached_stop.stop_loss`를 무조건 넣는다. 부분 진입 뒤 목표를 90 → 95로 올린 상태에서 같은 진입 주문의 늦은 체결이 오면 목표가 90으로 돌아간다.
   - 수정: 장부에 목표를 정한 에피소드 ID(`stop_episode_id`)를 같이 둔다.
     - 체결된 진입 주문의 에피소드가 `stop_episode_id`와 **같으면** 목표 = (기존 목표, 부착 손절가) 중 유리한 쪽.
     - **다르면**(새 에피소드) 목표 = 부착 손절가(새 초기 손절), `stop_episode_id`를 새 에피소드로 바꾼다.
   - 늦은 체결로 새로 붙은 부착 손절(trigger = 초기값)은 2번 주문별 맞추기로 현재 목표까지 올라간다.

### 만들지 않을 것

- S1 손절 계산 자체(M2-02), 실제 Bybit 연결(M3). Bybit에서 부착 Partial 손절의 trigger amend 후 부모 연결이 유지되는지는 M3-07 C3에서 확인한다.

## 완료 조건

- [x] `trail_stop`: 유리할 때만 목표 갱신, tick 정렬 방향(롱 내림·숏 올림), 청산 중이면 무시
- [x] 계약 불일치: BTC·ETH 모두 열린 에피소드인 상태에서 `trail_stop(symbol=BTC, spec=ETH 규칙)` → 거절, **양쪽** 장부·outbox·시도 기록 그대로
- [x] 시도 번호: 시도 0 TIMEOUT → 조회 결과 미적용 → **새 키(시도 1)**로 재시도가 실제로 전송됨(acknowledge된 옛 키·`_claimed`에 막히지 않음)
- [x] Accepted 뒤 이벤트 지연: 실제 trigger가 아직 옛 값이어도 판정 전 새 작업 없음 → 이벤트 도착 시 적용됨 / 끝내 안 오면 `retry_after` 뒤 조회로 판정
- [x] 재시작 경계: 전송 인수 직후·Accepted 직후·TIMEOUT 직후 각각 재시작 → `attempts`·`in_flight` 복원, 중복 전송 없음
- [x] 전송 직전 확인으로 버린 작업은 시도 횟수에 들어가지 않음
- [x] 같은 목표 반복: 목표 90 → 95, amend 거절 → 다음 판단의 원하는 값도 95 → `reconcile`이 같은 목표로 다시 `AMEND_STOP`(간격·한도 준수), 한도 초과 → 진입 정지·알림, 기존 손절 90 유지
- [x] amend 응답 유실 → 실제 적용된 경우 / 적용되지 않은 경우 각각: 판정 전 새 시도 없음, 적용됐으면 끝, 미적용이면 다음 시도
- [x] 늦은 OPEN: 미확인 손절은 작업 없음 → OPEN 확인 뒤 목표까지 amend
- [x] 오래 기다린 작업: 목표가 95 → 98로 바뀐 뒤 95짜리 작업 실행 → 보내지 않음(낮추지 않음), 98짜리 작업만 전송
- [x] trigger만 바꾼 amend `UNSUPPORTED` → D90 교체로 가지 않음, `Decimal(None)` 오류 없음, 저널 재생·재시작 포함
- [x] 늦은 진입 체결: 같은 에피소드 목표 95 유지, 새로 붙은 손절이 95로 맞춰짐 / 새 에피소드는 새 초기 손절. 롱·숏 대칭
- [x] 재시작: `TrailRecovery`·`stop_episode_id` 유지, 과거 trigger 재전송 없음(D90 재생)
- [x] 가상 거래소 통합: 일봉 3개 동안 목표가 올라가 거래소 손절이 따라 올라가고, 가격이 내려오면 갱신된 trigger에서 체결

## 검토 포인트

- 어떤 경로에서도 거래소 손절 trigger가 같은 에피소드 안에서 불리하게 바뀌지 않는지
- 목표 갱신(1번)과 주문 작업(2번)이 분리돼 있는지
- 진행 중 amend가 있을 때 중복 전송이 없는지

## 구현·검증 인수인계 (2026-10-22)

- `portfolio/trailing.py`가 주문별 시도 상태와 저널 인수·판정을 맡고, 동기화기가 실제 전송 및 성공한 조회의 시작 시각을 전달한다. 거절·미지원은 기존 손절을 유지한다.
- `tests/v0/test_trailing_stop.py` 19개 사례로 위 완료 조건과 REST 실패 중 지연 적용 알림, DB 실패 뒤 저널 복구를 검증한다. 실제 Bybit 동작 검증은 M3-07 C3에 남는다.
- 늦은 부분 진입의 자식 손절 알림이 체결보다 먼저 오면, 이전 보유 수량으로 `TRIM_STOP`이 예약될 수 있다. 같은 경로의 보호 수량 불변식을 지키기 위해 동기화기가 전송 직전 현재 보유와 다른 확인된 손절 잔량을 다시 읽는다. 현재 필요한 수량 아래로 줄이지 않으며, 초과분이 사라졌으면 작업을 버린다. 롱·숏 늦은 체결 통합 사례에 이 순서를 포함했다.
- 다음 호출자(M2-03/M3-04)는 계좌 UoW 안에서 대상 `symbol`, 해당 `InstrumentSpec`, `Price`를 전달하고 커밋한다. 목표 갱신 자체는 거래소를 호출하지 않는다. 재시작 때 `TrailRecovery`와 저널을 함께 복원해야 하며, Oracle 영속 구현은 M3-02 범위다.
