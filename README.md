# RichLetsGo — Bybit 선물 / 국내·미국 주식 자동매매

2026-10-19에 시작한 Codex + Claude Code 공동 개발 프로젝트입니다.

## 현재 상태

**M0와 M1 설계가 병합됐고, M1-01 도메인 모델을 구현하여 Claude Code 검토를 기다리고 있습니다.** 주문 실행 로직은 아직 없습니다. `trading_lab/`은 초기 오프라인 주문 검증 초안이며, 설계에 따라 새 패키지로 대체한 뒤 삭제합니다(D39). 실거래 준비가 완료된 프로그램이 아닙니다.

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

1. Claude: M1-01 구현 PR 검토 → 사용자 병합
2. M1-02 주문 상태·체결 반영부터 작업 명세 순서대로 구현
3. M2 백테스트 → M3 Bybit 데모 연결 → M4 AI → M5 대시보드

## 개발 환경 (M0-01)

운영 기준은 **Python 3.14**, 최소 지원은 **3.12**입니다(D40). 3.12 미만은 지원하지 않습니다. 이 PC는 설치된 3.14를 그대로 사용합니다. 아래 명령은 PowerShell에서 프로젝트 루트를 기준으로 실행합니다.

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
.\.venv\Scripts\python.exe -m mypy
.\.venv\Scripts\python.exe -m pytest
```

다른 개발 환경에서도 Python 3.12 이상으로 `.venv`를 만든 뒤 같은 설치·검사 명령을 사용합니다. 가상환경 활성화 없이 해당 Python을 직접 실행하므로 PowerShell 실행 정책을 바꿀 필요가 없습니다.

- CI는 모든 PR과 `main` push에서 Windows / Python 3.12·3.14 검사를 각각 실행합니다.
- `src/richletsgo/domain`에 공통 타입과 검증 함수가 있습니다. 나머지 패키지는 후속 작업의 기반이며 실행 서비스는 아직 없습니다.
- `trading_lab/`과 기존 테스트 9개는 M1 이전까지 유지합니다. 기존 테스트의 import를 위해 pytest의 `pythonpath = ["."]`를 임시 사용하며, 기존 패키지는 린트·타입 검사에서 제외합니다.
- `config.example.toml`은 비밀 값 없는 설정 예시입니다. 로컬 설정은 `config.local.toml`에 두며 Git에서 제외합니다. 위험 수치는 아직 미정이므로 예시에 실행 가능한 기본값을 넣지 않았습니다.
- 개발 도구는 테스트(`pytest`), 린트·포맷(`ruff`), 타입 검사(`mypy`)에만 사용합니다. 런타임 의존성은 없습니다.

## 키 보관

키는 채팅이나 저장소에 붙여넣지 않습니다. Windows 자격 증명 관리자에 보관하며, 설정 파일·DB·로그에 넣지 않습니다. 실거래 Bybit 키는 출금 권한 없이 만듭니다.

## 참고 문서

- Bybit V5 API: https://bybit-exchange.github.io/docs/v5/intro
- Bybit 데모: https://bybit-exchange.github.io/docs/v5/demo
- 한국투자증권: https://github.com/koreainvestment/open-trading-api

## 도메인 모델 (M1-01)

- `richletsgo.domain`에서 `Qty`, `Price`, `InstrumentSpec`, 이벤트, 열거형, 시계와 ID 함수를 가져옵니다. 수량·가격 래퍼의 Decimal은 `.value`로 읽으며, int·float·문자열의 암묵적 변환은 허용하지 않습니다.
- `Qty(Decimal("0"))`는 빈 포지션을 나타냅니다. 주문 방향 변환과 체결은 0 수량을 거절합니다. `PositionSnapshot.avg_price`는 빈 포지션에서 `None`일 수 있습니다.
- `InstrumentSpec`은 전달받은 step/tick의 배수로 내림·올림합니다. 가격 내림 결과가 0이면 유효한 `Price`가 아니므로 호출자가 진입을 거절해야 합니다. 손절 방향 선택과 위험 검사는 M1-03에서 구현합니다.
- 이벤트의 미확인 orderLinkId·부모 ID는 `None`입니다. 어댑터는 거래소의 빈 ID를 `None`으로 변환합니다. `OrderEvent.raw`는 원본을 복사한 읽기 전용 매핑이며, 중첩 배열은 tuple로 보관합니다. 원문 데이터는 거래 계산의 입력 검증과 구분합니다.
- `make_order_link_id(..., unique_factory=...)`에는 재시작을 포함해 고유한 ASCII 문자열 생성기를 주입합니다(접미사 1~29자, 전체 최대 36자). `new_operation_id(uuid_factory=...)`에는 UUID 생성기를 주입합니다. 도메인 모듈이 실제 시계나 난수를 직접 호출하지 않습니다.
- `FixedClock`과 `ManualClock`에는 UTC aware 시각을 전달합니다. `OrderState.rank`는 순서만 나타내며, 종결 상태 예외를 포함한 실제 상태 전이는 M1-02의 책임입니다.
