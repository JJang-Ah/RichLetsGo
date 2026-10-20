# M2-01 과거 시장 데이터 수집

- 상태: 명세 작성 완료, 사용자 확인 대기
- 구현: Codex / 검토: Claude Code
- 브랜치: `task/M2-01-market-data`
- 근거: D70(Bybit API, 3년), D8(BTCUSDT·ETHUSDT), D29(백테스트는 SQLite), 03 3.1절(경로 주입), 04 3.1절(1분봉 재검증)

## 목표

Bybit 공개 API에서 백테스트에 필요한 과거 데이터를 받아 검증하고 SQLite에 저장한다. 같은 명령을 다시 돌리면 빠진 구간만 이어 받는다.

## 범위

### 만들 것 (`src/richletsgo/marketdata/`)

1. **데이터 모델**
   - `Bar`: `symbol`, `interval`(`D`, `60`, `1`), `start`(UTC aware), `open`, `high`, `low`, `close`(Decimal), `volume`, `turnover`(Decimal). `high ≥ max(open, close)`, `low ≤ min(open, close)`, 모두 유한값.
   - `FundingRate`: `symbol`, `funding_time`(UTC), `rate`(Decimal).
   - `InstrumentSnapshot`: `symbol`, `qty_step`, `min_qty`, `tick_size`, `max_leverage`, `captured_at`. M1-01 `InstrumentSpec`으로 바꿀 수 있어야 한다.
   - `RiskLimitTier` (D79): `symbol`, 단계 번호, 단계 상한 명목 금액(USDT), 유지증거금률(MMR), 초기증거금률, `captured_at`. M1-03 강제청산 근사(D62)의 MMR 출처다.

2. **Bybit 공개 API 클라이언트** (읽기 전용, 키 없음)
   - 봉: `GET /v5/market/kline` (`category=linear`, `symbol`, `interval`, `start`, `end`, `limit`). 한 번에 받을 수 있는 개수 제한이 있으므로 기간을 나눠 반복한다.
   - 펀딩비: `GET /v5/market/funding/history`.
   - 상품 규칙: `GET /v5/market/instruments-info`.
   - 위험 한도 단계: `GET /v5/market/risk-limit` (`category=linear`, `symbol`). 수집 시점의 값만 얻을 수 있으므로 `captured_at`과 함께 저장한다(과거 시점 값이 아님을 보고서에 표시).
   - HTTP 클라이언트는 표준 라이브러리(`urllib`)로 하거나, 외부 패키지를 쓰려면 PR 설명에 이유를 적는다(런타임 의존성 추가).
   - 요청 간 간격과 재시도(지수 백오프, 최대 횟수)를 둔다. 응답의 `retCode`가 0이 아니면 오류로 처리한다.
   - 엔드포인트 기본 주소는 설정에서 받는다(메인넷 공개 API).
   - 응답 필드 순서·형식은 구현 시점의 Bybit 문서로 확인하고, 응답 원문 예시를 테스트 fixture로 저장한다.

3. **검증**
   - **확정된 봉만** 저장한다: 봉 시작 + 주기 ≤ 수집 시각인 봉만. 진행 중인 마지막 봉은 버린다.
   - 시작 시각이 주기 경계에 맞는지(UTC), 중복이 없는지, 연속인지 확인한다. 빠진 봉은 `gaps` 목록으로 기록하고 보고한다(빈 봉을 지어내지 않는다).
   - 가격이 0 이하이거나 OHLC 관계가 틀린 봉은 거절하고 기록한다.

4. **SQLite 저장소** (`storage/` 아래 SQLite 구현 또는 `marketdata/` 전용)
   - 파일 위치: 설정 `paths.backtest.market_data_dir`(이 작업에서 설정 예시에 키 추가) 아래 파일 하나. 경로는 주입받는다.
   - 테이블: `bars(symbol, interval, start, open, high, low, close, volume, turnover, PRIMARY KEY(symbol, interval, start))`, `funding(symbol, funding_time, rate, PRIMARY KEY(...))`, `instruments(...)`, `risk_limit_tiers(...)`, `ingest_runs(실행 시각, 범위, 받은 개수, gaps, 코드 커밋)`.
   - Decimal은 **TEXT**로 저장하고 읽을 때 Decimal로 복원한다(부동소수 손실 방지).
   - 같은 키가 다시 오면 값이 같으면 무시, 다르면 오류로 기록한다(조용히 덮어쓰지 않음).

5. **수집 명령**
   - `python -m richletsgo.marketdata.ingest --config <설정 파일> --symbols BTCUSDT ETHUSDT --intervals D 60 --since <UTC 날짜>` 형태.
   - 기본 기간은 D70(최근 3년). 이미 저장된 구간은 건너뛰고 빈 구간만 받는다.
   - `--intervals 1 --from ... --to ...`로 1분봉은 지정한 구간만 받는다(04 3.1절 재검증용).
   - 끝나면 받은 개수, 빠진 구간, 거절한 봉을 출력한다.

### 만들지 않을 것

- 실시간 WebSocket 시세 (M3), 미결제약정 수집 (M4), 지표 계산 (M2-02)

## 완료 조건

- [ ] 저장된 응답 fixture로: 기간 분할 반복, 진행 중 봉 제외, 중복 제거, 빠진 봉 보고, OHLC 이상 봉 거절
- [ ] Decimal TEXT 왕복(예: `0.10000000`의 자릿수 보존)
- [ ] 같은 키 다른 값 → 덮어쓰지 않고 오류 기록
- [ ] 다시 실행하면 빈 구간만 요청 (요청 범위를 테스트로 확인)
- [ ] `retCode` 오류, HTTP 오류, 시간 초과 → 재시도 후 실패 보고
- [ ] 저장 위치를 임시 폴더로 주입, cwd를 바꿔도 같은 곳에 저장 (03 3.1절)
- [ ] 테스트·CI에서 네트워크 접근 없음
- [ ] (수동) 실제 3년 수집 1회 실행 결과를 PR 설명에 첨부: 계약·주기별 개수, gaps

## 검토 포인트

- 진행 중 봉이 저장되는 경로가 없는지(미래 정보 차단의 출발점)
- 시간대 처리: Bybit의 ms 타임스탬프 → UTC aware
- 재실행 멱등성과 덮어쓰기 금지
