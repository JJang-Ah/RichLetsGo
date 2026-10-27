# M3-03 Bybit 어댑터

- 상태: 오프라인 구현·검증 완료(2026-10-26), Claude 검토 및 실제 데모 검증 대기
- 구현: Codex / 검토: Claude Code
- 브랜치: `task/M3-03-bybit-adapter`
- 근거: 03 3장(ExchangeGateway)·4.2~4.3절·5장, 01 1.4절(D41·D42), D53, D61, D71, D72, D79, D87, M1-05 인터페이스, M3-00의 데모 환경 사실

## 목표

M1-05의 `ExchangeGateway`를 Bybit V5로 구현한다. 가상 거래소와 같은 인터페이스·같은 이벤트 형식을 내서, 위·아래 로직이 거래소를 구분하지 않게 한다.

## 범위

### 만들 것 (`exchange/bybit/`)

1. **REST 서명 클라이언트**
   - 헤더 4종, HMAC-SHA256(`timestamp + api_key + recv_window + query 또는 JSON 본문`), `recv_window` 설정값.
   - 서버 시각(`/v5/market/time`)과 로컬 시각 차이를 시작 시와 주기적으로 측정해 기록. 차이가 설정 한도(예: 1초)를 넘으면 신규 진입 정지(`ENTRY_PAUSED_SYSTEM(clock)`)와 알림. 손절·청산은 계속.
   - 재시도: 네트워크 오류·5xx·속도 제한만. **주문 생성 요청은 재시도하지 않는다** — 응답이 없으면 `UNKNOWN`으로 넘기고(03 5.3절) orderLinkId 조회로 해소한다. 조회·취소·정정은 멱등이라 재시도 가능.
   - 오류 코드 매핑: 주문 없음/이미 종결/중복 orderLinkId/잔고 부족/수량·가격 규칙 위반/속도 제한 → 구조화된 결과(M1-05 타입).
2. **`place_order`** (D41·D53)
   - 내부 부호 수량 → `side` + 절대 수량은 여기서만 변환.
   - 진입: `stopLoss`, `tpslMode=Partial`, `slOrderType=Market`, `slTriggerBy`(설정, 기본 LastPrice) 필수. `reduceOnly`는 진입에 쓰지 않는다(Bybit는 reduceOnly 주문에 TP/SL을 붙일 수 없음).
   - 청산·보호: `reduceOnly=true`, 손절 재등록은 조건부 주문(`triggerPrice`, `triggerDirection`)으로.
   - `positionIdx=0`(단방향, D9). 시작 시 계약별 포지션 모드·격리 마진(D10)·레버리지(D11) 설정을 **읽어서 확인**하고, 다르면 진입 정지·알림(자동으로 바꾸지 않음).
   - 확인 출처: 통합 계좌(UTA)의 격리 여부는 포지션의 `tradeMode`(지원 중단)가 아니라 `/v5/account/info`의 `marginMode`로 본다. 레버리지는 `/v5/position/list`의 `leverage`. 매핑 표를 코드 옆 문서에 남긴다.
3. **조회** (03 4.3절 동기화용)
   - 미체결: 일반 주문과 조건부 주문(`orderFilter`) 모두. 주문 이력, 체결 내역(`/v5/execution/list`, 시작 시각 기준 페이지), 포지션, 지갑(`/v5/account/wallet-balance` → `AccountSnapshot(wallet_balance, unrealized_pnl)`, D72), 입출금·이체 기록(`/v5/account/transaction-log` → `CashFlow`, D71).
   - **데모는 주문 기록을 7일만 보관**: 동기화 재조회 범위가 7일을 넘으면 자동 복구하지 않고 정지 유지·알림(03 4.3절 마지막 규칙의 구체 값).
   - 응답을 `OrderEvent`(M1-02 확장 필드 전부: symbol, order_qty, order_type, time_in_force, limit_price, trigger_price, reduce_only, stop_kind, parent_order_link_id)와 `ExecutionEvent`로 바꾼다. `stopOrderType` → `StopKind` 매핑을 표로 문서화한다.
4. **개인 WS** (`stream-demo.bybit.com/v5/private`)
   - 인증(`GET/realtime{expires}` HMAC), 구독 `order`·`execution`·`position`·`wallet`, 20초 ping, 끊김 감지 시 지수 백오프 재연결(5분 500회 제한 아래), 재연결마다 M1-06 동기화 절차 호출.
   - 같은 이벤트 중복·순서 뒤바뀜은 M1-02 규칙이 처리하므로 어댑터는 받은 그대로 원문과 함께 넘긴다.
5. **공개 시세 WS** (메인넷 `stream.bybit.com/v5/public/linear`)
   - `kline.60.{symbol}`, `kline.D.{symbol}` 구독, **`confirm=true`인 봉만** 확정 봉으로 넘긴다. 펀딩 기록은 REST로 주기 조회(D87).
   - 끊김·공백은 M1-03 `MarketDataGuard`가 판단하고, 보충은 M2-01 수집기의 REST 경로를 재사용.
6. **상품·위험 단계**: 시작 시와 하루 1회 `instruments-info`·`risk-limit` 조회(M2-01 클라이언트 재사용 가능), `InstrumentSpec`·1단계 MMR(D79)을 위험 엔진에 공급.

### 만들지 않을 것

- WS 주문(데모 미지원), 실거래 주소 사용(M3는 `live` 시작 거부)

## 완료 조건

- [ ] 서명: Bybit 문서 예시 값으로 서명 문자열·결과가 같은지 테스트
- [ ] 응답→이벤트 변환: 실제 데모 응답을 fixture로 저장해(키 등 비밀 값 제거) `OrderEvent`·`ExecutionEvent` 변환 테스트. 부착 손절 응답에 `parentOrderLinkId`가 있는 경우·없는 경우 모두
- [x] 주문 생성 응답 없음 → 재시도하지 않고 `UNKNOWN` 결과
- [x] 포지션 모드·마진·레버리지가 설정과 다르면 진입 정지
- [x] 시각 차이 초과 → 진입 정지, 손절·청산 경로는 영향 없음
- [x] 공개 WS: `confirm=false` 봉은 확정 봉으로 넘기지 않음
- [ ] (수동) 데모 계정에서 최소 수량 진입(부착 손절) → 손절 확인 → 청산까지 1회, WS 끊고 재연결 1회 결과를 PR에 첨부

## 검토 포인트

- 주문 생성에 자동 재시도가 없는지(중복 주문 방지의 핵심)
- 진입 요청에 손절이 빠질 수 있는 경로가 없는지
- 데모/메인넷 주소가 섞이지 않는지(시세는 메인넷 공개, 주문은 데모)


## 구현 검증 기록 (2026-10-26)

- 코드 옆 [V5 매핑·연결 계약](../../src/richletsgo/exchange/bybit/MAPPING.md)에 오류/계좌/주문/이벤트/WS 및 D90 필드 대응을 기록했다.
- 오프라인 테스트 44개: 실제 전송 query/JSON의 서명, 고정 HMAC 벡터, create 응답 유실 무재시도, 페이지/커서 오류, Partial SL 소유권 유무, 전체 수량 정정, 시각/설정 오류, 재연결 reader와 동기화 동시 실행, 기존 M1-06 실제 조정자 연결.
- 서명 테스트는 공식 프로토콜의 canonical 문자열과 가짜 키로 계산한 값 및 공개 HMAC 벡터를 대조했다. 공식 예시의 실제 키/서명 쌍 또는 실제 데모 응답 녹화로 검증한 것으로 표시하지 않았다. 위 수동 fixture/서명 예시 완료 항목은 그대로 남긴다.
- 키 등록/읽기·실제 데모 주문은 수행하지 않았다. Oracle 설치 전 M3-02와 새 전략 V1 통과 전 M3-07은 시작하지 않는다.
