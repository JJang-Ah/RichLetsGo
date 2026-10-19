# RichLetsGo — Bybit 선물 / 국내·미국 주식 자동매매

2026-10-19에 시작한 Codex + Claude Code 공동 개발 프로젝트입니다.

## 현재 상태

**설계 단계**입니다. 매매 기능 코드는 아직 없습니다. `trading_lab/`은 초기 오프라인 주문 검증 초안이며, 설계에 따라 새 패키지로 대체한 뒤 삭제합니다(D39). 실거래 준비가 완료된 프로그램이 아닙니다.

## 설계 문서

설계 문서가 구현의 기준입니다. 결정 번호(D1~)와 남은 질문(Q~)은 `00_DECISIONS.md`에서 관리합니다.

| 문서 | 내용 |
|---|---|
| [docs/design/00_DECISIONS.md](docs/design/00_DECISIONS.md) | 확정된 결정, 설계 원칙, 남은 질문 |
| [docs/design/01_STRATEGY_AND_AI.md](docs/design/01_STRATEGY_AND_AI.md) | 전략(S1 스윙 추세추종, S2 단기 평균회귀)과 AI 점수 |
| [docs/design/02_RISK_MANAGEMENT.md](docs/design/02_RISK_MANAGEMENT.md) | 한도 계층, 크기 계산, 정지 상태, 텔레그램 명령 |
| [docs/design/03_SYSTEM_ARCHITECTURE.md](docs/design/03_SYSTEM_ARCHITECTURE.md) | 프로세스, 모듈, 데이터 흐름, 주문 상태, Oracle 저장소 |
| [docs/design/04_VALIDATION_AND_OPERATIONS.md](docs/design/04_VALIDATION_AND_OPERATIONS.md) | 백테스트 → 데모 → 실거래 단계, 운영 체크리스트 |
| [docs/design/05_DEVELOPMENT_PROCESS.md](docs/design/05_DEVELOPMENT_PROCESS.md) | 역할 분담, PR 흐름, 마일스톤 |

## 범위 요약

- 첫 대상: Bybit USDT 무기한 BTCUSDT·ETHUSDT, 단방향, 격리 마진, 레버리지 3~5배 범위.
- 완전 자동매매, 사용자의 Windows PC에서 Windows 서비스로 실행, 주 저장소는 로컬 Oracle Database Free.
- AI 점수는 진입을 막거나 크기를 줄이기만 하며, 손절·한도는 규칙 엔진이 강제합니다.
- 한국투자증권 국내·미국 주식은 Bybit 실거래가 안정된 뒤 별도 설계로 추가합니다.

## 협업 방식

- 사용자: 결정, PR 병합. Claude Code: 설계·검토. Codex: 구현 (D37).
- 모든 변경은 브랜치 + PR + 다른 AI의 검토 후 병합합니다. `main`에 직접 커밋하지 않습니다 (D38).
- 두 AI 사이의 연락은 PR 코멘트와 [docs/HANDOFF.md](docs/HANDOFF.md)로 합니다.
- 공통 규칙: [docs/WORKING_RULES.md](docs/WORKING_RULES.md). `CLAUDE.md`와 `AGENTS.md`가 이 규칙을 가리킵니다.

## 다음 순서

1. Claude: M0(기반) 작업 명세 작성 → 사용자 확인
2. Codex: M0 구현 (Python 3.12 이상 / 운영 3.14, `pyproject.toml`, CI, `.gitignore` 정리)
3. M1 핵심 로직 → M2 백테스트 → M3 Bybit 데모 연결 → M4 AI → M5 대시보드

## 키 보관

키는 채팅이나 저장소에 붙여넣지 않습니다. Windows 자격 증명 관리자에 보관하며, 설정 파일·DB·로그에 넣지 않습니다. 실거래 Bybit 키는 출금 권한 없이 만듭니다.

## 참고 문서

- Bybit V5 API: https://bybit-exchange.github.io/docs/v5/intro
- Bybit 데모: https://bybit-exchange.github.io/docs/v5/demo
- 한국투자증권: https://github.com/koreainvestment/open-trading-api
