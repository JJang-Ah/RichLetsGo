# 작업 인계

## 2026-10-19 — Codex

- 사용자 요청: 한국투자증권 국내·미국 주식 + Bybit 선물, Claude Code와 공동 개발.
- 계정 상태: KIS API 키 보유, Bybit 계정 보유. 실제 키와 계정에는 접근하지 않음.
- 단방향/헤지 모드 차이를 설명했으며, 사용자의 최종 선택은 아직 없음.
- 완료: 협업 지침, 설계 초안, 네트워크 없는 주문 검증 예제 및 단위 테스트.
- 다음 담당자: Claude Code 검토. README의 검토 요청문을 사용.
- 미완료: 모든 거래소 연동, 전략, 모의 체결·손익, 대시보드, 운영 AI 연결, 실거래.
- 검증: Python 3.12에서 주문 검증 테스트 9개 통과. CLI 출력의 CP949 인코딩 문제를 ASCII 문구로 수정하고 예시 실행 확인.

## 2026-10-19 — Claude Code (설계)

- 사용자 요청: 코딩을 멈추고 설계부터 확정. 설계는 사용자와 Claude가 진행.
- 완료: `docs/design/00~05` 작성 (결정 D1~D40, 원칙 P1~P5). README, WORKING_RULES, ARCHITECTURE.md를 설계 기준으로 갱신.
- 핵심: Bybit BTC/ETH USDT 무기한부터, 완전 자동, 이 PC에서 Windows 서비스, Oracle Database Free 주 저장소, AI는 진입을 막거나 줄이기만 함.
- 역할 변경: Claude 설계·검토, Codex 구현. 모든 변경은 브랜치 + PR + 교차 검토 (main 직접 커밋 금지).
- 검증: 문서 변경만. 코드·테스트 변경 없음.
- 사용자 결정 대기: Q9~Q18, Q20~Q21 (수치와 운영 세부). 수치(Q13, Q21)는 백테스트 결과를 보고 정함.
- 다음 담당자: Claude가 M0 작업 명세 작성 → 사용자 확인 → Codex 구현.
- 참고: PR #1(docs/claude-review)의 REVIEW.md 발견 사항은 M0·M1 명세에 반영 예정.

## 2026-10-20 — Claude Code (PR #2 검토 반영)

- Codex 검토 코멘트 11건(P1 5, P2 6)을 모두 반영. 새 결정 D41~D51 (00_DECISIONS.md "2026-10-20 추가").
- 주요 변경: 진입 주문에 Partial 손절 부착(D41), 손절의 전략 귀속과 청산 절차(D42), 승인 목표·signal_id 멱등(D43), 주문 상태 규칙과 UNKNOWN 해소(D44), DB 장애 중 보호 작업 저널(D45), 위험 게이트 검사+예약(D46), 개인 WS 선구독 동기화(D47), HALTED 재개 시 위험 기준 재설정(D48), 가상 체결 규칙(D49), 독립 그림자 장부(D50), AI 모드(D51).
- Python 안내를 D40(운영 3.14, 최소 3.12) 기준으로 05와 M0 명세에 정리.
- 검증: 문서 변경만. Bybit 공식 문서(create-order, trading-stop, private order WS)로 Partial 손절 수량·parentOrderLinkId·Filled 중복을 확인.
- 다음 담당자: Codex 재검토. M3 데모에서 01 1.4절 "M3 데모에서 확인할 것" 항목을 실제로 확인해야 함.

## 2026-10-20 — Claude Code (PR #2 재검토 반영)

- Codex 재검토 6건(P1 3, P2 3) 반영. 새 결정 D52~D56, D17·D42·D44 개정.
- 사용자 결정: 청산 순서를 "손절 유지 → 청산 확인 → 손절 취소"로 변경(D42). AI 입력에서 계좌 상태 제외(D17)하고 신호별 점수를 실제 계좌·그림자 장부가 공유(D56).
- 그 밖: 미반영 체결 노출(D52), 수량 부호 계약(D53), NOT_FOUND 확정 조건(D54), 저널 작업 ID(D55). V0 시나리오와 M1 완료 조건 보강.
- 검증: 문서 변경만.
- 다음 담당자: Codex 재검토.

## 2026-10-20 — Claude Code (D54 재개정)

- Codex 후속 확인 1건 반영: UNKNOWN 자동 부재 확정(NOT_FOUND) 제거. 거래소 상태 확인, 진입 주문의 orderLinkId 취소 성공, 사용자 수동 확정(`/resolve`, M3)으로만 종결하고 그 전까지 예약·진입 정지 유지.
- V0에 "빈 조회·포지션 일치가 지속된 뒤 늦은 OPEN 도착" 등 4건 추가.
- 검증: 문서 변경만. 다음 담당자: Codex 재확인.

## 2026-10-19 — Claude Code

- 사용자 요청: 현재 저장소를 파악하고 부족한 점을 검토해 docs/REVIEW.md에 정리, 결정 사항은 질문으로 남기기.
- 완료: docs/REVIEW.md 작성. 코드·테스트·.gitignore는 수정하지 않음.
- 주요 발견: 한도를 넘는 포지션의 전량 청산이 주문 한도로 거절됨(core.py:79), 단방향 반전 주문 허용, 심볼 정규화·수량 정밀도 검사 없음, .gitignore 패턴이 소스 파일까지 제외할 수 있음, Limits/position/side 검증 테스트 없음.
- 검증: `py -3 -m unittest discover -s tests -v` (Python 3.14.3) → 9개 통과. 발견 1~4는 validate 직접 호출로 재현.
- 사용자 결정 대기: REVIEW.md의 질문 1~9 (청산 한도 면제, 반전 허용, Bybit 모드, KIS 계좌, 운영 환경, 알림, 저장소 공개, Python 버전, 협업 방식).
- 다음 담당자: 질문 1·2 결정 후 core.py 수정 담당(Codex 또는 Claude) 지정, 다른 쪽이 검토. .gitignore 정리와 누락 테스트 추가는 결정과 무관하게 진행 가능.

## 2026-10-20 — Codex (PR 병합 및 M0-01 구현)

- 사용자 요청: Claude 최종 수정 재검토, PR #1·#2 병합, main 보호 설정, M0 구현.
- 최종 설계 검토: 91bcfb6의 D54 재개정 및 V0 기대 결과 확인. 앞선 재검토 항목은 설계 수준에서 반영됨. Partial 손절의 실제 동작 검증은 M3 과제.
- PR #1·#2를 병합했다. docs/HANDOFF.md 충돌은 PR #1 검토 기록과 PR #2 설계·후속 수정 기록을 모두 보존해 해결했다.
- 구현 브랜치: task/M0-01-project-foundation. pyproject.toml, src 패키지 17개, 빈 마이그레이션 디렉터리, 설정 예시, CI, 도구 설정, .gitignore/.gitattributes, 개발 환경 안내를 추가했다.
- 개발 의존성 pytest/ruff/mypy는 테스트·린트/포맷·타입 검사 용도이며 런타임 의존성은 없다.
- 검증: Windows 로컬 Python 3.14.3 및 3.12.14에서 ruff check, ruff format --check, mypy 통과. pytest는 각각 28개 통과(기존 9개 + 스모크 19개). 초기 pytest 캐시 경고는 로컬 캐시 디렉터리 생성 후 해소됨.
- 기존 trading_lab 런타임 파일은 변경하지 않았다. tests/test_core.py는 줄바꿈 포맷만 바꿨고 AST가 기존과 같음을 확인했다.
- git check-ignore 확인: keystore 소스·config.example.toml·하위 marketdata/data 소스는 추적 가능, config.local.toml·.env·루트 data/x.csv·.venv 산출물은 제외됨. TOML 예시 파싱 및 위험 값 미설정 확인.
- main 보호: GitHub 화면에 비공개 저장소의 규칙 강제 요금제 제한이 표시됨. 저장에는 계정 본인 확인(sudo mode)도 요구됨. 실제 강제가 확인되기 전에는 보호 완료로 간주하지 않는다.
- 다음 담당자: Claude Code가 M0-01 PR을 검토. CI 두 버전 통과를 확인한 뒤 사용자가 병합한다. M0 구현 PR은 Codex가 병합하지 않는다.
