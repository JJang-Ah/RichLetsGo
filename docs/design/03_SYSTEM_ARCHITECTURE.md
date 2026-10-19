# 시스템 구조

결정 번호는 [00_DECISIONS.md](00_DECISIONS.md), 전략은 [01_STRATEGY_AND_AI.md](01_STRATEGY_AND_AI.md), 위험 관리는 [02_RISK_MANAGEMENT.md](02_RISK_MANAGEMENT.md)를 따른다.

## 1. 기술 선택

| 항목 | 선택 | 비고 |
|---|---|---|
| 언어 | Python 3.12 + asyncio (D28, D40) | 금액·수량은 Decimal. |
| 주 저장소 | Oracle Database Free, 이 PC에 설치 (D29) | SQL Developer로 관리. 접속은 `python-oracledb` thin 모드(별도 클라이언트 설치 불필요). |
| 백테스트 저장소 | SQLite 파일 | 실거래 DB와 분리. 결과 비교용. |
| 화면 | 로컬 웹 대시보드, 읽기 전용 (D30) | `127.0.0.1`에서만 접속. 주문 기능 없음. |
| 실행 | Windows 서비스 (D31) | 부팅 시 자동 시작, 비정상 종료 시 재시작. |
| 비밀 정보 | Windows 자격 증명 관리자 | API 키, 봇 토큰, DB 비밀번호. 설정 파일·DB·로그에 넣지 않는다. |
| 시간 | 내부는 모두 UTC, 화면·알림만 한국 시간 | |

## 2. 프로세스 구성

| 프로세스 (Windows 서비스) | 역할 | 의존 |
|---|---|---|
| `OracleServiceFREE` (Oracle 설치 시 생성) | 주 저장소 | |
| `RichLetsGo-Trader` | 시세·전략·AI·위험·주문·텔레그램. 매매 로직은 전부 여기 있다. | Oracle |
| `RichLetsGo-Watchdog` | Trader와 Oracle 상태 감시, 이상 시 텔레그램 알림 | 없음 (Oracle이 죽어도 동작해야 함) |
| `RichLetsGo-Dashboard` | 읽기 전용 웹 화면 | Oracle (읽기 전용 계정) |

- Trader는 `127.0.0.1`에 상태 확인 주소(health)를 연다. Watchdog은 이 주소와 Oracle 접속을 일정 간격으로 확인한다. DB를 거치지 않기 때문에 Oracle이 멈춘 경우에도 알릴 수 있다.
- Watchdog은 주문 권한이 없다. 알림만 보낸다.
- Oracle에 기록할 수 없으면 Trader는 신규 진입을 멈춘다(주문 기록 없이 주문하지 않는다). 손절 주문은 거래소 서버에 있으므로 영향이 없다.

## 3. 모듈 구성 (Trader 내부)

```
trading/
  config/        설정 로딩(TOML), 환경 구분(backtest / demo / live)
  keystore/      자격 증명 관리자 접근 (`secrets`는 표준 라이브러리 이름과 겹치고 .gitignore 패턴에도 걸려서 피함)
  exchange/      ExchangeGateway 인터페이스
    bybit/       REST, 공개 WebSocket(시세), 개인 WebSocket(주문·체결·포지션)
    simulated/   백테스트용 가상 거래소 (같은 인터페이스)
  marketdata/    봉 생성·검증, 공백 감지, 저장
  strategy/      S1 추세추종, S2 평균회귀. 입력(봉, 상태) → 목표 포지션. 거래소·DB 직접 접근 금지
  portfolio/     전략별 목표 합산(D14), 전략 장부
  ai/            Scorer 인터페이스, 공급자 어댑터, 비용 집계, 응답 형식 검증
  risk/          L1~L7 검사, 크기 계산, 정지 상태 관리
  oms/           주문 관리자: 주문 상태 전이, 손절 주문 관리, 거래소와 맞추기
  storage/       저장소 계층: Oracle 구현, SQLite 구현(백테스트)
  notify/        텔레그램 알림·명령
  health/        상태 확인 주소
  app.py         조립과 실행 순서
watchdog/
dashboard/
db/migrations/   버전 번호가 붙은 SQL 파일 (V001__init.sql …)
```

원칙
- 전략, 위험 엔진, 장부는 **순수 로직**으로 만든다. 거래소·DB·시계는 인터페이스로 주입한다. 그래야 백테스트·데모·실거래에서 같은 코드가 돈다.
- 실거래·데모·백테스트는 설정의 `environment` 값 하나로 고르고, DB 스키마와 API 키도 환경별로 완전히 분리한다.

## 4. 데이터 흐름

### 4.1 진입

```
봉 마감 (공개 WS)
 → marketdata: 봉 확정·검증·저장
 → strategy: S1/S2 목표 포지션
 → portfolio: 합산, 이전 목표와 비교해 주문 의도(증가분) 생성
 → ai: 진입 의도에 대해 점수 (주기 평가 결과 + 신호 평가)
 → risk: 크기 계산, L1~L7 검사, 정지 상태 확인
 → oms: 주문을 DB에 먼저 저장 → 거래소에 전송 → 손절 주문 등록
 → 모든 단계의 입력·결과를 decision 기록으로 남김
```

### 4.2 체결과 동기화

```
개인 WS (주문·체결·포지션)
 → oms: 주문 상태 갱신, 체결 저장
 → portfolio: 전략 장부 갱신
 → risk: 평가액·손실·연속 손실 갱신 → 필요하면 정지 상태 변경
 → notify: 체결·정지 알림
```

- 개인 WS가 끊기면 신규 진입을 멈추고, 재연결 후 REST로 주문·포지션 전체를 다시 읽어 맞춘다.
- 일정 간격(예: 1분)으로 REST 조회와 장부를 비교하는 정기 대조도 돌린다.

### 4.3 시작 순서

1. 설정·비밀 정보 로딩, 환경 확인 (live이면 시작 시 텔레그램으로 알림)
2. Oracle 접속, 마이그레이션 버전 확인 (맞지 않으면 시작 중단)
3. 저장된 정지 상태 복원
4. 거래소에서 잔고·포지션·미체결 주문 조회 → 장부·주문 테이블과 대조
5. 모든 포지션의 손절 주문 확인, 없으면 등록
6. 시세 WS 연결, 필요한 과거 봉 보충
7. 신규 진입 허용 (정지 상태가 없을 때)

## 5. 주문 상태

모든 주문은 거래소에 보내기 **전에** DB에 저장하고, 우리 쪽 주문 ID(`orderLinkId`)를 붙인다. 응답이 늦거나 끊겨도 이 ID로 거래소에 조회해 중복 주문을 막는다.

```
CREATED ──전송──→ SUBMITTED ──접수 확인──→ OPEN ──일부 체결──→ PARTIALLY_FILLED
   │                 │                      │                       │
   │                 │ 응답 없음/시간 초과  ├──→ FILLED ←──────────┘
   │                 ▼                      ├──→ CANCELLED (일부 체결 포함 가능)
   │              UNKNOWN ──ID로 조회──→ (OPEN/FILLED/CANCELLED/REJECTED 중 하나로 확정)
   │                                        └──→ REJECTED / EXPIRED
   └──→ ABORTED (전송 전 위험 검사 실패나 정지)
```

- 상태가 바뀔 때마다 `order_events`에 한 줄씩 추가한다(덮어쓰지 않음).
- `UNKNOWN` 주문이 하나라도 있으면 해당 계약의 신규 진입을 멈춘다.
- 주문 종류: 진입(지정가/시장가), 손절(서버 조건부, reduce-only), 익절(reduce-only), 청산(reduce-only 시장가). 주문마다 소속 전략을 기록한다.

## 6. 저장소 (Oracle)

### 6.1 스키마와 계정

| 계정 | 용도 | 권한 |
|---|---|---|
| `RLG_DEMO` | 데모 환경 데이터 | 소유자 |
| `RLG_LIVE` | 실거래 데이터 | 소유자 |
| `RLG_READ` | 대시보드, SQL Developer 조회 | 두 스키마 SELECT만 |

데모와 실거래 데이터는 서로 다른 스키마에 둔다(WORKING_RULES: 실거래와 데모 수치를 섞지 않는다).

### 6.2 주요 테이블 (초안)

| 테이블 | 내용 |
|---|---|
| `bars` | 계약, 주기, 시작 시각, OHLCV, 확정 여부 |
| `market_snapshots` | 펀딩비, 미결제약정 |
| `news_items` | 출처, 게시 시각, 수집 시각, 제목, 본문 요약, 원문 해시 |
| `decisions` | 전략 신호 → 합산 → AI → 위험 검사 → 결과까지 한 건의 판단 기록 |
| `ai_calls` | 공급자, 모델, 프롬프트 버전, 입력 해시, 응답, 토큰, 비용, 지연 시간, 오류 |
| `orders` | orderLinkId, 거래소 주문 ID, 전략, 종류, 방향, 수량, 가격, 현재 상태 |
| `order_events` | 주문 상태 변경 이력 |
| `fills` | 체결 ID, 주문, 가격, 수량, 수수료 |
| `strategy_positions` | 전략별 장부 포지션 (수량, 평균가, 손절가) |
| `equity_snapshots` | 평가액, 지갑 잔고, 미실현 손익 (주기 + 거래일 경계) |
| `cash_flows` | 입출금 |
| `risk_states` | 정지 상태, 이유, 시작·해제 시각 |
| `commands` | 텔레그램 명령, 보낸 사람, 처리 결과 |
| `alerts` | 보낸 알림 |
| `config_versions` | 실행에 쓰인 설정 내용과 해시 |

- 금액·수량은 `NUMBER`(정밀도 지정 없이)로 저장해 Decimal과 그대로 오가게 한다. 시각은 UTC `TIMESTAMP`.
- 스키마 변경은 `db/migrations`의 SQL 파일로만 한다. SQL Developer에서 직접 바꾸지 않는다(조회는 자유).

### 6.3 백업

- Oracle Data Pump로 매일 내보내기, 일정 기간 보관 (보관 기간 Q18).

## 7. 대시보드 (읽기 전용)

- 화면: 현재 포지션과 손절가, 평가액·낙폭 그래프, 한도 사용률, 정지 상태, 주문·체결 목록, decision별 AI 점수와 근거, AI 비용 누계.
- `RLG_READ` 계정만 쓴다. 주문·설정 변경 기능은 넣지 않는다. 제어는 텔레그램 명령(D27)으로만 한다.

## 8. 아직 정하지 않은 것

| # | 질문 |
|---|---|
| Q16 | 대시보드 웹 프레임워크 (예: FastAPI + 간단한 HTML, Streamlit 등) |
| Q17 | 이 PC 사양 확인: Oracle Free + Trader + Dashboard를 함께 돌릴 메모리 여유 |
| Q18 | 백업 보관 기간과 위치 (같은 디스크 / 외장 / 클라우드) |
| ~~Q19~~ | Python 기준 버전 → D40 (3.12)로 확정 |
