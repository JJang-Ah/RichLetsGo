# M3 데모 연결 — 작업 개요

- 근거: [05_DEVELOPMENT_PROCESS.md](../design/05_DEVELOPMENT_PROCESS.md) 3장 M3, [03_SYSTEM_ARCHITECTURE.md](../design/03_SYSTEM_ARCHITECTURE.md), [02_RISK_MANAGEMENT.md](../design/02_RISK_MANAGEMENT.md) 6장, [04_VALIDATION_AND_OPERATIONS.md](../design/04_VALIDATION_AND_OPERATIONS.md) 5·7장
- 선행 조건: M1 전체(M1-04~06 포함), M2-02(전략 신호) 병합
- 구현: Codex / 검토: Claude Code (D37, D86)

## 목표

M1·M2의 순수 로직에 실제 외부 연결(Bybit 데모, Oracle, 텔레그램, Windows 서비스)을 붙여 **AI 없이(`ai.mode=disabled`) 데모 계정에서 1주 무사고 운영**한다. 이 단계에서 Bybit의 실제 동작(부착 손절, 수량 자동 조정, 강제청산가, 수수료)을 확인하고 설계와 다르면 설계를 먼저 고친다.

## Bybit 데모 환경에서 확인한 사실 (2026-10-20, 공식 문서)

| 항목 | 내용 | 설계 영향 |
|---|---|---|
| 주소 | REST `https://api-demo.bybit.com`, 개인 WS `wss://stream-demo.bybit.com`. **공개 시세 WS는 데모에 없고** 메인넷 `wss://stream.bybit.com`과 같은 데이터를 쓴다 | 시세는 메인넷 공개 WS, 주문·체결은 데모 개인 WS (M3-03) |
| 지원 범위 | 주문·정정·취소·조회, 포지션(레버리지·모드·trading-stop), 계좌(지갑·입출금 내역), 개인 WS(order·execution·position·wallet). **WS 주문은 미지원** | 주문은 REST만 (M3-03) |
| 주문 보관 | 데모 주문 기록은 **7일** 보관 | 동기화(D47)의 체결 재조회 범위가 7일을 넘으면 자동 복구하지 않고 수동 확인 (M3-04) |
| 데모 자금 | `POST /v5/account/demo-apply-money` (분당 1회) | 운영 안내서 (M3-07) |
| 인증 | 헤더 `X-BAPI-API-KEY`·`TIMESTAMP`·`SIGN`·`RECV-WINDOW`, HMAC-SHA256(시각+키+recv_window+쿼리/본문). 시각은 `서버시각 − recv_window ≤ ts < 서버시각 + 1000` | PC 시간 동기화 필수(D36), 서버 시각 차이 감시 (M3-03) |
| WS 유지 | 20초마다 `ping` 권장, 끊기면 즉시 재연결, 5분에 500회 넘게 연결하지 말 것 | 재연결 백오프 (M3-03) |

## 작업 순서와 의존 관계

| 작업 | 내용 | 의존 |
|---|---|---|
| [M3-01](M3-01-config-and-keystore.md) | 설정 로더(환경별·경로·검증), 자격 증명 관리자 접근, 구조화 로그 | M1 |
| [M3-02](M3-02-oracle-storage.md) | Oracle 스키마·마이그레이션, `UnitOfWork` Oracle 구현, 백업(Data Pump) | M1-02·03·06 |
| [M3-03](M3-03-bybit-adapter.md) | Bybit 데모 REST·개인 WS·공개 시세 WS 어댑터(`ExchangeGateway` 구현) | M1-05, M3-01 |
| [M3-04](M3-04-trader-assembly.md) | Trader 조립: 시세→전략→위험→주문→동기화 실행 루프, 상태 확인 주소 | M1 전체, M2-02, M3-01~03 |
| [M3-05](M3-05-telegram.md) | 텔레그램 알림·명령(`/status` `/pause` `/resume` `/cancel` `/resolve`), 키 만료 알림 | M3-04 |
| [M3-06](M3-06-watchdog-and-services.md) | Watchdog, Windows 서비스 등록·자동 재시작, 운영 체크 | M3-04, M3-05 |
| [M3-07](M3-07-demo-run.md) | 데모 1주 운영, Bybit 실제 동작 확인 기록, M3 완료 판정 | M3-01~06 |

## M3 공통 규칙

- 데모와 실거래를 섞지 않는다: 주소·API 키·Oracle 스키마(`RLG_DEMO`/`RLG_LIVE`)·경로가 `environment` 하나로 함께 바뀐다. `live`는 M3에서 **시작 자체를 거부**한다(실거래 전환은 04 4.2절 조건 충족 후 별도 작업).
- 비밀 정보(API 키·시크릿, 봇 토큰, DB 비밀번호)는 자격 증명 관리자에서만 읽는다. 로그·DB·예외 메시지·텔레그램에 남기지 않는다(마스킹 테스트).
- 외부 연동 코드는 M1의 인터페이스(`ExchangeGateway`, `UnitOfWork`, `Clock`)를 구현한다. 순수 로직을 고치지 않는다. 고쳐야 하면 설계·명세 질문부터.
- 런타임 의존성을 추가할 때는 PR 설명에 이유를 적는다. 이 명세가 제안하는 의존성: `aiohttp`(REST·WS·텔레그램), `oracledb`(thin 모드), `keyring`(Windows 자격 증명 관리자). 다른 선택이 낫다면 구현 전에 질문.
- 실제 외부 연결 테스트는 CI에서 돌리지 않는다. 수동 통합 테스트 절차를 각 명세에 적고, 결과를 PR에 첨부한다.

## M3 완료 조건 (05 3장 M3 그대로 + 보완)

- 데모에서 `ai.mode=disabled`로 **1주 무사고**: 해결 안 된 UNKNOWN 0, 장부-거래소 불일치 0, 손절 없는 포지션 0.
- 그 기간에 실제 수행: 진입·청산 각 1건 이상, 손절 체결 또는 의도적 손절 테스트 1건, 프로세스 재시작 복구 1회, 개인 WS 끊김 복구 1회. 부분 체결이 데모에서 생기지 않으면 V0 테스트로 대체했다고 기록.
- 01 1.4절 "M3 데모에서 확인할 것"과 아래 M3-07 확인 항목의 결과를 기록하고, 설계와 다르면 설계 PR을 먼저 낸다.
- V0 표 중 M3 담당 부분(7·20·26·27·33·36행의 외부 연동) 확인.

## 미정 (사용자 확인 필요)

| # | 질문 |
|---|---|
| Q22 | Windows 서비스 등록 도구: NSSM(별도 실행 파일 설치, 설정 단순) / pywin32 서비스(파이썬 의존성, 설치 복잡). 제안은 NSSM |
| Q23 | Oracle Database Free 설치·계정 생성은 사용자가 직접(관리자 권한 필요). 설치 안내는 M3-02에 포함 |
