# Bybit V5 매핑 및 연결 계약 (M3-03)

2026-10-26 공식 문서를 대조한 오프라인 구현이다. `tests/test_bybit_adapter.py`의 응답은 문서 필드로 만든 합성 예시이며 **실제 데모 계정 녹화가 아니다**. 키 준비 후 실제 응답 fixture·최소 주문·재연결 수동 검증을 추가해야 한다.

## 연결과 실행 순서

- `RestClient(BybitConfig, ApiCredential, Clock, Transport, sleep)` → `BybitGateway(rest, PauseState, alert)`를 만든다. 운영 transport는 호출자가 수명 관리하는 `aiohttp.ClientSession`과 `AiohttpTransport`이다. import·생성자에서 네트워크/자격 증명 접근을 하지 않는다.
- REST 주문/개인 조회는 설정의 `api-demo.bybit.com`, 개인 WS는 `stream-demo.bybit.com/v5/private`, 시세 REST/WS는 메인넷 공개 주소만 허용한다. 서명 헤더의 HTTP redirect 전달을 금지한다.
- `await gateway.initialize()`로 시각·계좌 설정·상품/위험 단계를 확인한다. 이후 `gateway.maintain()`을 서비스 task로 실행해 설정한 간격의 시각 검사와 하루 한 번 상품/tier 갱신을 유지한다. 실패·기한 경과 시 진입을 거부한다.
- `StreamRunner.private(gateway, synchronizer.synchronize)`가 인증·구독 완료 후 이벤트 reader를 시작하고 **매 연결마다** 설정/시각 재확인과 M1-06 동기화를 호출한다. reader는 동기화 REST 조회와 동시에 실행되며 `private_events()` 큐를 통해 기존 동기화기로 전달한다. 동기화가 실패하면 다시 연결하고 진입 정지를 유지한다. 소비자는 하나다.
- 연결 단절/종료 시 connected=false 및 `bybit_stream` 정지. 손절/청산 요청은 연결·진입 readiness 검사로 막지 않는다. 응답 유실은 성공으로 간주하지 않는다. 다른 위험 정지는 상위 위험 게이트/동기화기가 적용한다.
- `StreamRunner.public(on_bar, on_connection)`은 확정 D/60 봉만 전달한다. 서비스는 연결 상태·봉 시각을 MarketDataGuard에 전달하고 공백 보충/펀딩 조회에 기존 `marketdata.bybit_public.BybitPublicClient`와 수집 경로를 사용한다. 동기 REST 수집을 서비스 이벤트 루프 안에서 직접 실행하지 않는다. 스케줄·Oracle 저장·서비스 조립은 M3-04 범위다.
- 서비스 종료 시 stream/maintain task 취소 → `Synchronizer.close()` → HTTP session close. WS heartbeat는 설정값(20초 이하), pong 대기는 같은 간격이며 실패하면 재연결한다. 지수 백오프는 최소 1초·설정 상한으로 한 프로세스에서 5분 500회 미만이다. 동일 계좌 서비스 중복 실행 금지는 M3-04/06에서 보장한다.

## 주문/오류

| 내부 값 | V5 전송 또는 수신 |
|---|---|
| 부호 Qty | Buy/Sell + abs(qty), `positionIdx=0` |
| ENTRY | 필수 `stopLoss`, `tpslMode=Partial`, `slOrderType=Market`, `slTriggerBy=LastPrice`; reduceOnly 생략 |
| CLOSE | reduceOnly=true, 시장가 기본; 조건부 close 거절 |
| PROTECT | reduceOnly=true, triggerPrice + triggerDirection(매수 1/매도 2) + triggerBy |
| D90 amend_stop(qty) | 부호/보호 주문 식별 검증 뒤 **abs(전체 수량)**. 누적 체결을 빼지 않는다. trigger만 전달하면 qty 생략, qty만 전달하면 trigger 생략 |
| cancel/amend 식별 | 캐시의 거래소 ID 우선. 신규/UNKNOWN은 제출 때 보관한 symbol+orderLinkId. 재시작 캐시가 없으면 완전한 미체결 조회로 namespace와 symbol을 찾고, 추측한 ID/계약으로 쓰지 않는다 |
| HTTP timeout/network/5xx/속도 제한 | 조회·취소·정정만 제한 횟수 재시도. create는 **항상 1회**. 쓰기 결과 불명은 TIMEOUT → 기존 OMS UNKNOWN |
| 10014 / 110072 중복 ID | TIMEOUT. 이미 수락된 주문일 수 있으므로 확정 거절/새 ID 재전송으로 취급하지 않음 |
| 110001 / 110008·110010 | 명령 NOT_FOUND / TOO_LATE; 조회 실패는 QUERY_FAILED |
| amend 110059 / 110060 | 해당 TP/SL 변경 금지 → UNSUPPORTED. D90 수량 변경이면 기존 교체 보호 절차가 처리 |
| 10001·수량/가격/잔고 오류 | REJECTED. 포괄적인 인수 오류를 수량 변경 미지원으로 추측하지 않음 |
| 성공 응답 ID 누락/불일치 | TIMEOUT. 명령 수락은 체결 확정이 아니며 WS/조회로 확인 |

retMsg/요청/인증 원문을 오류에 넣지 않는다. 시각 오차는 왕복 중간 시각을 기준으로 구하고 반 왕복 시간을 불확실성으로 더한다. `richletsgo.Trader` 로그에 숫자 offset/판정을 남긴다. 로컬 시스템 시각을 자동 수정하지 않는다.

## 이벤트/계좌

| V5 | 내부 |
|---|---|
| New / Untriggered / Triggered | OPEN |
| PartiallyFilled / Filled | PARTIALLY_FILLED / FILLED |
| Cancelled / PartiallyFilledCanceled / Deactivated / Rejected | CANCELLED / CANCELLED / CANCELLED / REJECTED |
| PartialStopLoss | PARTIAL_STOP_LOSS; parentOrderLinkId는 있을 때만 보존, 없으면 추측하지 않음 |
| StopLoss | STOP_LOSS |
| Stop / TakeProfit / PartialTakeProfit / TrailingStop | CONDITIONAL. TP의 parent는 SL 소유권으로 넘기지 않고 raw에만 보존. 프로그램은 거래소 TP/native trailing을 생성하지 않음 |
| 빈 stopOrderType / UNKNOWN | stop_kind=None |
| order 이벤트 | symbol, 부호 qty, orderType, timeInForce, limit/triggerPrice, reduceOnly, stop_kind, 원문 및 ID 전부 변환 |
| execution Trade/AdlTrade/BustTrade | ExecutionEvent, 수수료 음수 리베이트 보존. Funding은 체결 수량으로 처리하지 않음 |
| position | positionIdx=0만 허용; REST avgPrice / WS entryPrice, 빈 포지션은 Qty(0)·평단 None |
| account/info.marginMode | ISOLATED_MARGIN 확인. position.tradeMode 사용하지 않음 |
| position/list.leverage | 설정 레버리지와 일치 검사. 자동 설정 변경 없음 |
| wallet UNIFIED.coin[USDT] | walletBalance와 unrealisedPnl 각각 AccountSnapshot. USD 계좌 전체 totals는 격리 마진에서 사용하지 않음. 차입 잔액 거절 |
| transaction-log TRANSFER_IN/OUT | USDT change의 부호를 보존한 CashFlow. category 필터를 걸어 이체를 누락하지 않음 |
| TRADE/SETTLEMENT/LIQUIDATION/ADL/DELIVERY/FEE_REFUND | 입출금에서 제외. 모르는 자산 이동 종류는 QUERY_FAILED·정지하여 일손익 기준을 임의로 보정하지 않음 |

중복/역순 이벤트를 제거하거나 정렬하지 않는다. 주문 원문은 불변 `OrderEvent.raw`, 체결은 execId로 기존 OMS가 멱등 처리한다. private wallet 알림은 `wallet_events` 별도 큐이며 `PrivateEvent` 계약을 바꾸지 않는다. WS 지갑 알림만으로 매 시세의 미실현 손익을 갱신할 수 없으므로 서비스의 REST 계좌 조회가 여전히 필요하다.

미체결은 Order/StopOrder 두 필터의 모든 페이지를 조회한다. 조회 결과 누락/모르는 enum/커서 반복을 빈 정상 결과로 반환하지 않는다. 이력·체결·입출금에는 고정 startTime/endTime과 모든 페이지를 사용하며 요청 범위가 현재 기준 7일을 넘으면 정지/알림한다. M3-04에서 SyncPolicy.history_window는 네트워크 처리 시간을 고려해 7일보다 작은 값을 명시해야 한다. 정확히 now-7일로 시작하면 다음 REST 호출 시 이미 보존 범위를 벗어날 수 있다. 실제 7일 초과 복구는 허용 폭으로 덮지 않는다.

## 남은 수동 확인

- 실제 데모 subaccount 응답에서 attached SL의 parentOrderLinkId 유무 및 stopOrderType 확인, 비밀 제거 fixture 추가.
- Partial SL 전체 수량 변경 성공/미지원 응답(C10), 부착 SL 생성·수량 자동 조정, 최소 진입→보호 확인→청산, WS 재연결 1회.
- USDT 격리 지갑/이체·레버리지 응답, 계정별 권한·운영 환경 확인. 키는 현재 테스트에 사용하지 않았다.
- D94 새 전략 V1 통과 전에는 M3-07 1주 운영으로 진행하지 않는다.

공식 근거: [서명](https://bybit-exchange.github.io/docs/v5/guide), [데모](https://bybit-exchange.github.io/docs/v5/demo), [주문](https://bybit-exchange.github.io/docs/v5/order/create-order), [정정](https://bybit-exchange.github.io/docs/v5/order/amend-order), [조회](https://bybit-exchange.github.io/docs/v5/order/open-order), [오류](https://bybit-exchange.github.io/docs/v5/error), [WS](https://bybit-exchange.github.io/docs/v5/ws/connect), [지갑](https://bybit-exchange.github.io/docs/v5/account/wallet-balance), [거래 기록](https://bybit-exchange.github.io/docs/v5/account/transaction-log), [enum](https://bybit-exchange.github.io/docs/v5/enum).
