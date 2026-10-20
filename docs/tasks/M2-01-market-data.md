# M2-01 과거 시장 데이터 수집

- 상태: 구현 완료, Codex 검토 대기 (해석 확인: 사용자 2026-10-20, I6 → D87, I7은 실제 응답 확인 후 정정)
- 구현: **Claude Code** / 검토: **Codex** (D86 예외: Codex 사용 한도, 독립·저위험 작업, 명세는 PR #8에서 Codex 검토 완료)
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

- [x] 저장된 응답 fixture로: 기간 분할 반복, 진행 중 봉 제외, 중복 제거, 빠진 봉 보고, OHLC 이상 봉 거절
- [x] Decimal TEXT 왕복(예: `0.10000000`의 자릿수 보존)
- [x] 같은 키 다른 값 → 덮어쓰지 않고 오류 기록
- [x] 다시 실행하면 빈 구간만 요청 (요청 범위를 테스트로 확인)
- [x] `retCode` 오류, HTTP 오류, 시간 초과 → 재시도 후 실패 보고
- [x] 저장 위치를 임시 폴더로 주입, cwd를 바꿔도 같은 곳에 저장 (03 3.1절)
- [x] 테스트·CI에서 네트워크 접근 없음
- [x] (수동) 실제 3년 수집 1회 실행 결과를 PR 설명에 첨부: 계약·주기별 개수, gaps

## 검토 포인트

- 진행 중 봉이 저장되는 경로가 없는지(미래 정보 차단의 출발점)
- 시간대 처리: Bybit의 ms 타임스탬프 → UTC aware
- 재실행 멱등성과 덮어쓰기 금지

## 구현 해석 (Claude Code, 2026-10-20, 구현 전 확인 대기 — D86)

Codex가 멈춘 동안 Claude가 구현한다(D86 예외: 독립·저위험, 명세는 PR #8에서 Codex 검토 완료). **아래 해석을 Codex 또는 사용자가 확인한 뒤에 코드를 쓴다.** 검토는 Codex가 한다.

Bybit 문서 확인 결과(2026-10-20): kline은 최대 1000개, 최신순 정렬, **진행 중인 봉도 포함**(종가 = 마지막 체결가). 펀딩 이력은 최대 200개, **startTime만 주면 오류**, endTime만 주면 그 시각까지 200개. 펀딩 주기는 계약마다 다를 수 있고 instruments-info에 있다. risk-limit은 cursor 페이지, 증거금률이 **퍼센트로 보이는** 예시(문서에 단위 명시 없음).

| # | 항목 | 해석 |
|---|---|---|
| I1 | 모듈 구성 | `marketdata/models.py`(Bar, FundingRate, InstrumentSnapshot, RiskLimitTier), `marketdata/bybit_public.py`(HTTP 클라이언트), `marketdata/validate.py`, `marketdata/store.py`(SQLite, 백테스트 전용이라 `storage/`가 아니라 `marketdata/`에 둔다), `marketdata/ingest.py`(명령). |
| I2 | HTTP | 표준 라이브러리 `urllib`만 쓴다(런타임 의존성 추가 없음). 전송 계층을 인터페이스로 주입해서 테스트는 저장된 응답 fixture만 쓴다. 기본 주소는 설정에서 받는다. |
| I3 | 설정 읽기 | 설정 로더는 M3 범위다. M2-01 명령은 `tomllib`로 필요한 키만 읽는다: `paths.backtest.market_data_dir`, `[bybit_public] base_url`. 두 키를 설정 예시에 추가한다. 상대 경로는 설정 파일 부모 폴더 기준(03 3.1절). |
| I4 | 봉 수집 | 오래된 쪽부터 최대 1000개 단위로 `start`/`end`를 정해 요청하고, 최신순 응답을 시간순으로 정렬한다. **봉 시작 + 주기 > 수집 시각인 봉(진행 중)은 버린다.** 수집 시각은 주입한 시계에서 받는다. 주기는 `D`, `60`, `1`만 허용한다. |
| I5 | 펀딩 수집 | `endTime`만 주고 뒤로 페이지를 넘긴다(200개씩). 받은 가장 오래된 시각보다 1ms 앞을 다음 `endTime`으로 쓰고, 목표 시작 시각보다 오래되면 멈춘다. 정렬은 문서에 없으므로 받은 뒤 직접 정렬한다. |
| I6 | 펀딩 주기 (설계 질문) | instruments-info의 `fundingInterval`(분)을 `InstrumentSnapshot`에 함께 저장한다. **D76·D77은 펀딩 시각을 00·08·16시로 고정했는데, Bybit는 계약마다 주기가 다를 수 있다.** 제안: M2-03 완전성 검사와 가상 거래소는 고정 시각 대신 **저장된 펀딩 기록의 실제 시각**을 쓰고, 이웃한 두 기록의 간격이 수집 시점 주기보다 길면 빠진 것으로 본다. 이 해석이 맞으면 D76·D77 문구를 고치는 문서 PR을 따로 낸다. |
| I7 | 위험 한도 단계 (2026-10-20 정정) | `risk-limit`을 cursor로 끝까지 받고 요청 계약만 남긴다. **실제 linear 응답을 확인한 결과 증거금률은 퍼센트가 아니라 비율이다**(BTCUSDT 1단계: MMR `0.0033`, IMR `0.0066`, maxLeverage 150 → 0.0066×150 = 0.99). 그래서 값을 그대로 비율로 저장한다(처음 해석의 ÷100은 틀렸음). 단위 확인을 위해 `초기증거금률 × maxLeverage ≈ 1`(오차 2% 이내)을 검사하고, 맞지 않으면 저장하지 않고 실패로 보고한다. 단계 번호는 `riskLimitValue` 오름차순 순서. |
| I8 | 상품 규칙 | `lotSizeFilter.qtyStep`, `minOrderQty`, `priceFilter.tickSize`, `leverageFilter.maxLeverage`, `fundingInterval`. M1-01 `InstrumentSpec`으로 바꾸는 함수를 둔다. |
| I9 | 검증 | 가격 0 이하, OHLC 관계 위반, 주기 경계에 안 맞는 시작 시각은 거절하고 기록한다. 같은 응답 안 중복 시작 시각은 값이 같으면 하나로, 다르면 오류. 빠진 봉은 연속 구간 `[시작, 끝]` 목록으로 보고한다. 빈 봉을 만들지 않는다. |
| I10 | 저장 | 설정 폴더 아래 `market_data.sqlite3` 파일 하나. Decimal은 `str(Decimal)`로 TEXT 저장(자릿수 보존). 같은 키가 다시 오면 값이 같으면 건너뛰고, 다르면 덮어쓰지 않고 그 실행을 오류로 끝낸다(충돌 목록은 `ingest_runs`에 기록). 한 실행의 저장은 계약·주기 단위 트랜잭션으로 한다. |
| I11 | 재실행 | 계약·주기별로 저장된 봉을 보고 `[since, 지금]` 중 비어 있는 구간만 요청한다. 1분봉은 `--from`/`--to`를 줄 때만 받는다. 기본 `since`는 수집 시각 − 3년(D70). |
| I12 | 재시도 | 요청 간격과 최대 재시도 횟수, 지수 백오프를 명령 인자로 받는다. **이 값들은 위험 수치가 아니라서 기본값을 둔다**(예: 간격 0.2초, 최대 5회). `retCode ≠ 0`, HTTP 429·5xx, 시간 초과는 재시도하고, 그 밖의 4xx는 바로 실패. 최종 실패는 보고하고 0이 아닌 종료 코드로 끝낸다. |
| I13 | 실행 기록 | `ingest_runs`: 시작·끝 시각, 계약·주기·범위, 받은/저장/거절 개수, 빠진 구간, 충돌, 패키지 버전, 코드 커밋. 커밋은 `--commit` 인자로 받고, 없으면 `unknown`(별도 프로세스 호출 없음). |
| I14 | 시간 | Bybit ms 타임스탬프는 모두 UTC aware `datetime`으로 바꾼다. 명령의 날짜 인자도 UTC로만 받는다. |
