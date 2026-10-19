# RichLetsGo — 국내·미국 주식 / Bybit 선물

2026-10-19에 시작한 Codex + Claude Code 공동 개발 프로젝트입니다.

## 현재 상태

외부 패키지 없이 실행하는 **오프라인 주문 검증 초안**입니다. 시세 수집, 계좌 조회, 거래소 연결, 주문 전송, 체결, 손익 계산 및 매매 전략은 아직 없습니다. 출력 가격은 실제 시세가 아닌 고정 예시입니다. 실거래 준비가 완료된 프로그램이 아닙니다.

```powershell
python -m trading_lab
python -m unittest discover -s tests -v
```

Python 3.11 이상을 사용합니다. 이 PC의 기본 python 실행 경로가 작동하지 않아 Codex에 포함된 Python 3.12로 검증했습니다.

## 확정 범위와 임시 기준

- 한국투자증권: 국내주식 + 미국주식. 사용자는 기존 API 키 보유.
- Bybit: 선물. 계정 보유, API 키는 아직 없음.
- 개발 협업: Codex와 Claude Code. 현재 Claude가 검토하거나 연결된 상태는 아님.
- 검증용 선물 모델: USDT 선형 무기한, 단방향 포지션. 사용자 확정 전 임시 기준.
- 전략, 종목, 자금, 거래 주기, 실거래 위험 한도는 미정.
- 예시 금액/레버리지 한도는 검증용 숫자이며 운용 권고가 아님.

## 다음 개발 순서

1. Claude Code로 아래 검토 요청을 실행하고, 발견 사항을 공유.
2. 한국투자증권 API 종류·모의투자 신청 여부, 해외주식 권한을 확인하고 읽기 전용 연결.
3. Bybit 상품/포지션 모드 확정 후 데모 계정과 전용 키로 읽기 전용 연결.
4. 실제 시세 및 상품별 수량 단위/호가 단위/최소 주문 조건을 검증.
5. 재시작 가능한 주문·체결 기록, 계좌 동기화, 부분 체결 및 중복 방지 구현.
6. 전략 한 개와 수수료·슬리피지·환율·펀딩비를 반영하는 모의매매 구현.
7. 사용자와 자금/위험 한도를 정한 후 실거래 연결을 별도 검토.

## Claude Code에서 시작하기

이 폴더를 작업 폴더로 열고 아래 내용을 입력하세요. `CLAUDE.md`와 `AGENTS.md`는 공통 규칙을 가리킵니다.

> README.md, docs/ARCHITECTURE.md, docs/WORKING_RULES.md, docs/HANDOFF.md를 읽고 현재 주문 검증 코드와 테스트를 검토해줘. 실주문 연결은 추가하지 말고, 금액 단위 혼동, 잘못된 청산 방향, 입력 검증, 중복 주문 및 테스트의 빈틈을 중심으로 확인해줘. 발견 사항을 파일·위치·영향과 함께 docs/REVIEW.md에 작성해줘. 수정이 필요하면 변경 파일 목록과 검증 결과를 기록해줘.

한 사람이 구현하는 동안 다른 AI는 검토를 맡습니다. 같은 파일을 동시에 편집하지 않습니다. 검토 결과를 이 채팅에 알려주면 다음 작업에 반영할 수 있습니다. 두 도구 사이의 자동 메시지 전달은 아직 없습니다.

## 키 보관

키는 채팅이나 저장소에 붙여넣지 않습니다. 후속 연결 단계에서 로컬 환경변수 또는 OS 자격증명 저장소를 사용합니다. 현재 코드는 키를 읽지도 않습니다. `.gitignore`에 비밀 파일 제외 규칙을 준비했습니다.

## 참고 문서

- 한국투자증권: https://github.com/koreainvestment/open-trading-api
- Bybit 주문 규격: https://bybit-exchange.github.io/docs/v5/order/create-order
- Bybit 데모: https://bybit-exchange.github.io/docs/v5/demo
- Claude Code 지침: https://support.claude.com/en/articles/14553240-give-claude-context-claude-md-and-better-prompts
