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
## 2026-10-20 — Claude Code (M0 검토, M1 명세)

- PR #3(M0-01) 검토: 명세와 일치, 병합 차단 문제 없음. 이 PC Python 3.14.3에서 ruff·format·mypy·pytest(28) 통과. 제안: 패키지 탐색 `include = ["richletsgo*"]`(M1-01에 반영), `[ai] mode` 예시는 M3.
- GitHub Actions 장애(2026-10-19 githubstatus "Degraded availability")로 PR #3 CI가 30분 넘게 대기. **사용자 지시로 CI를 기다리지 않고 병합**(76cdefc, D38 예외). 근거: Codex가 3.12·3.14에서, Claude가 3.14에서 ruff·format·mypy·pytest(28) 통과. 병합 후 main에서도 3.14로 다시 확인. 장애가 풀리면 main의 CI 결과를 확인해야 함.
- M1 작업 명세 작성: `docs/tasks/M1-00-overview.md`와 M1-01~06. 03 모듈 목록에 `domain/` 추가.
- 다음 담당자: 사용자 명세 확인 → Codex가 M1-01부터 구현(M1-05는 M1-01 이후 병행 가능).

## 2026-10-20 — Claude Code (PR #4 Codex 검토 반영)

- Codex 검토 8건(P1 4, P2 4) 반영. 새 결정 D57~D63 (00_DECISIONS.md).
- 설계: 진입 주문만 노출 증가(D57), 청산 후 남은 전략 보호 먼저 복구(D58), 진입 에피소드(D59), decision·주문 원자성과 전송 규칙(D60), 체결 귀속 보류(D61), 강제청산 근사식·여유 부등식(D62), 저널 손상 처리(D63).
- 명세: M1-02~06 보완, M1-00에 V0 담당 표(37행, M1 정책 vs 이후 외부 연동) 추가.
- 검증: 문서 변경만. 다음 담당자: Codex 재검토.

## 2026-10-20 — Claude Code (PR #4 재검토 반영)

- Codex 재검토 2건(P1) 반영: 에피소드 종료 조건과 `EXITING`(D64), 저널 정상/긴급 전송 구분과 거래소 대조 복구(D65, D63 개정). V0 표 37·38행 추가(총 39행).
- 작업 규칙 추가: PR 코멘트 첫 줄에 작성자 표시 (`**[Claude Code]**` / `**[Codex]**`). 사용자 요청.
- 검증: 문서 변경만. 다음 담당자: Codex 재검토.

## 2026-10-20 — Codex (PR #4 최종 검토 및 M1-01)

- f186fef의 D64·D65와 V0 추가 시나리오를 재검토했고, 앞선 두 지적의 반영을 확인했다. 두 Python CI 성공 후 사용자 지시에 따라 PR #4 병합(abcf7f4).
- `task/M1-01-domain-model`에서 도메인 모듈 7개 구현: 열거형, Decimal 래퍼, 상품 단위 정렬, 불변 이벤트, 주입형 ID 생성, UTC 시계. 패키지 탐색을 `richletsgo*`로 보완했다.
- 입력 계약: Qty/Price는 Decimal 전용, 빈 포지션 평균가는 None 허용, 미확인 ID는 None, raw는 복사 후 중첩까지 읽기 전용, ID 생성기는 호출자가 주입. 런타임 의존성 추가 없음.
- 검증: Python 3.14.3·3.12.14에서 ruff check/format, mypy strict(24 소스) 통과. pytest 각각 161개 통과(기존 28 + 도메인 133). 기존 trading_lab은 변경하지 않았다.
- 기본 빌드 격리를 사용하는 pip wheel 성공. 비격리 빌드 시도는 로컬 venv에 setuptools가 없어 실패했으며, 선언된 빌드 환경으로 재실행하여 성공했다.
- 다음 담당자: Claude Code가 M1-01 PR을 검토. 사용자 병합 후 M1-02 진행. 상태 전이/위험 엔진/실제 거래소/DB 연결은 이번 범위에 없음.

## 2026-10-20 — Codex (경로 하드코딩 방지)

- 사용자 요청: 파일·폴더 경로를 하드코딩하지 않기. 현재 제품 소스·테스트에 PC 절대 경로가 없음을 확인했다.
- 공통 규칙, 설계 03 3.1절, 설정 예시와 M1-06 완료 조건에 반영. 설정 파일 부모 기준 상대 경로, 환경별 설정, Path 주입, 임시 디렉터리·cwd 변경 검증을 명시했다.
- 고정 위치로 읽힐 수 있던 data/journal 문구를 설정 키 참조로 변경했다. 설정 로더는 기존 일정대로 M3이며 이번 변경에 런타임 로더는 추가하지 않았다.
- 검증: TOML 파싱, 세 환경의 네 경로 설정 및 분리 확인, git diff --check. 문서·설정 예시 변경이라 런타임 테스트는 추가하지 않았다.
- 다음 담당자: Claude Code가 경로 설정 계약을 검토하고 M1-06/M3 구현 시 적용.

## 2026-10-20 — Claude Code (PR #5·#6 검토·병합, M1-02 명세 보완)

- PR #5(M1-01) 검토 후 사용자 지시로 병합(a312d72). PR #6(경로 설정)은 main과 충돌(README·HANDOFF) 해결 후 병합.
- M1-01 명세 누락 보완: M1-02 명세에 `OrderEvent` 확장(symbol, order_qty, order_type, time_in_force, limit_price, trigger_price, reduce_only, stop_kind)을 첫 항목으로 추가.
- PR #6 검토 제안 반영: "저널 불확실" 표시 파일 위치를 저널 디렉터리 안 `JOURNAL_UNCERTAIN`으로 정함. 재시작 동기화는 표시와 관계없이 항상 D65 받아들이기 대조 실행(03 5.4절, M1-06).
- 남은 PR #6 제안(demo·live의 backtest_results_dir, Oracle DIRECTORY 객체 메모, 목록 빈 줄)은 M3 설정 로더 작업 때 정리.
- 검증: 문서 변경만. 다음 담당자: Codex 명세 검토 후 M1-02 구현.


## 2026-10-20 — Codex (PR #7 검토 및 M1-02 구현)

- Claude의 PR #7(2881003)을 검토해 병합 차단 사항이 없다는 리뷰를 남겼다. OrderEvent 확장과 저널 표시 파일 위치·표시와 무관한 복구 대조를 확인했다. PR #7은 아직 열려 있어 구현 PR은 docs/M1-02-orderevent를 기준으로 한다. 명세 PR 병합 후 구현 PR의 기준을 main으로 변경한다.
- task/M1-02-order-state-and-fills: OrderEvent 확장, 주문 상태 전이, exec_id 멱등 반영, 귀속 보류·외부 확정, UNKNOWN 해소 판단과 수동 확정, 노출 합계, 저장소 프로토콜 및 계좌별 메모리 UoW를 구현했다.
- 체결·주문·훅 변경을 함께 커밋하고 예외·취소·미커밋 종료 시 버린다. 메모리 트랜잭션은 계좌별 잠금으로 직렬화한다. 팩토리 반환 타입으로 UoW 프로토콜 적합성도 검사한다.
- 훅은 UoW 저장소만 변경한다. 알림 플래그·최초 UNKNOWN/미반영 시각·수동 확정 감사 기록의 보존은 호출자 책임이며 README에 명시했다. 전략 장부(M1-04), 거래소(M1-05/M3), 저널(M1-06), Oracle(M3)은 후속 범위다.
- 검증: Python 3.14.3·3.12.14에서 ruff check/format, mypy strict(30 소스), pytest 각각 211개 및 28 subtests 통과(기존 161 + 신규 50). 동시 중복 체결, 롤백·커밋 실패·태스크 취소, 통지 선후 역전, 전략별 손절 귀속, UNKNOWN 유지, 늦은 체결 정정을 포함한다.
- PC 절대 경로·외부 접근·런타임 의존성을 추가하지 않았다. 다음 담당자: Claude Code 구현 PR 검토 후 사용자 병합. Codex는 구현 PR을 병합하지 않는다.

## 2026-10-20 — Claude Code (미정 질문 정리, M2 명세)

- Codex가 한도로 멈춘 동안 사용자와 미정 질문을 정리: D66~D74 (전략 후보 4개 조합, S2 1시간봉, 손절 방식, AI 외부 정보는 경제 일정만, 과거 데이터 Bybit 3년, 입출금 자동 반영, 거래일 기준값은 지갑 잔고, 대시보드 FastAPI+HTML, 백업 로컬 14일). Q17 PC 사양 확인 완료.
- 남은 질문: Q13 위험 수치(백테스트 후), Q20 고정 IP(사용자 확인 중), Q21 V1 통과 수치(V1 실행 전).
- M2 작업 명세 작성: `docs/tasks/M2-00`~`M2-04`. 03 모듈 목록에 `backtest/` 추가.
- 주의: 작업 폴더에 Codex의 M1-02 미커밋 작업(task/M1-02-order-state-and-fills)이 있어 Claude는 별도 worktree에서 작업함.
- 검증: 문서 변경만. 다음 담당자: Codex 명세 검토, M1 완료 후 M2-01부터(M2-01은 M1-01 이후 병행 가능).
- 추가(같은 날): 사용자가 고정 IP 확인 없이 진행하기로 결정 → D75 (API 키 IP 제한 없음, 출금 권한 없음·2단계 인증·키 만료 알림으로 보완). Q20 종료.

## 2026-10-20 — Claude Code (PR #8 Codex 검토 반영)

- PR #7: Codex 검토 통과(구현 차단 문제 없음). Codex는 이 명세로 M1-02 구현 중.
- PR #8 검토 7건(P1 3, P2 4) 반영: D76 경계 시각 처리 순서, D77 데이터 완전성(누락 구간 무효), D78 구간마다 독립 실행·끝에서 청산·합성 곡선, D79 MMR은 Bybit 위험 한도 1단계(수집 시점), D80 선택 점수 0 분모·동률·후보 없음 처리. D72를 M1-03 입력 계약(`AccountSnapshot`: 지갑 잔고 + 미실현)과 M1-05(`get_account`)에 반영. 볼린저·RSI 비교 시점을 인덱스로 고정.
- 검증: 문서 변경만. 다음 담당자: Codex 재검토.

## 2026-10-20 — Claude Code (PR #8 재검토 반영)

- Codex 재검토 3건 반영: D81 일일 손익 식(입출금 부호 수정, 손실 = max(0, −손익)), D78 개정(모든 실행은 자기 구간 안 마지막 봉 시가에 청산, 구간 밖 가격 사용 금지, D77 범위도 [시작 − 워밍업, 끝)), D79 보완(1단계 상한은 계약 합산 노출로 위험 게이트 안에서 검사).
- 검증: 문서 변경만. 다음 담당자: Codex 재확인 후 병합.


## 2026-10-20 — Codex (PR #9 P2 수정 및 main 병합)

- origin/main(55ecd16, PR #7·#8 포함)을 병합하고 HANDOFF 충돌은 Codex 구현 기록과 Claude 설계·검토 기록을 모두 보존했다. PR #9의 기준은 main이다.
- 귀속 보류 체결의 signed pending_qty와 절댓값×체결가 합계 pending_notional을 추가하고 total_notional에 포함했다. 반대 방향 보류 체결도 상쇄하지 않는다. 귀속 확정 시 보류분이 제거되며 롤백 시 유지된다. 귀속 불명이라 전략 필터에도 보수적으로 포함하고 계약 필터는 적용한다.
- resolve_unknown에 last_cancel_at/cancel_retry_after 필수 입력을 추가했다. NOT_FOUND/TOO_LATE/ERROR는 간격 경과 후 재시도, ACCEPTED와 보호 주문은 재시도하지 않는다. 최초 취소와 알림 1회 동작, 예약 유지, 실제 상태에 의한 해소는 유지한다. 호출자는 실제 시도 시각을 보존해야 한다.
- 회귀 테스트 21개 추가: 롱·숏 보류 노출, 반대 체결 합산, 중복/귀속 확정/롤백, 계약·전략 필터, 실패 종류별 재시도 경계·다음 시도, 보호 주문/ACCEPTED 제외, 알림과 재시도, 입력 시각 검증.
- 검증: Python 3.14.3·3.12.14에서 ruff check/format, mypy(30 소스), pytest 232개 및 28 subtests 통과. 다음 담당자: Claude Code가 PR #9 수정 검토 후 사용자 병합.

## 2026-10-20 — Claude Code (M1-03 구현, 역할 교대)

- 사용자 요청: Codex가 사용 한도로 멈춰 Claude가 M1-03을 대신 구현. 구현자≠검토자 원칙(D37)을 지키기 위해 이 PR은 **Codex가 검토**.
- `risk/`: records(결정·에피소드), limits, sizing(D62 근사), equity(D72·D81), pause(직렬화), checks(L2~L7, 축소 면제), guards, resume(D48), gate(D46·D59·D60). `storage`에 DecisionRepository·EpisodeRepository와 메모리 구현 추가(UnitOfWork 프로토콜 확장).
- 검증: Python 3.14.3에서 ruff check/format, mypy strict(39 소스), pytest 291 passed(신규 59). 3.12는 이 PC에 없어 CI로 확인.
- 설계 해석(검토 요청): ① 한도는 "닿으면" 정지(≥). ② 배분 0인 전략은 L4로 항상 정지. ③ 입출금은 그것이 반영된 스냅샷과 함께 넘긴다(따로 넘기면 일시적 가짜 손실). ④ HALTED 재개 시 위험 기준 최고점은 확인 메시지에 보여 준 값(현재 평가액이 더 높으면 그 값). ⑤ 정지 갱신은 게이트 잠금 안, 트랜잭션 밖(입력에서 다시 계산되는 값이라 커밋 실패와 무관).
- 남은 일: M1-04(포트폴리오·보호 주문)는 이 게이트 위에서 진행. M1-05는 병행 가능.


## 2026-10-20 — Codex (PR #10 지적 6건 수정)

- 사용자 요청으로 Codex가 리뷰 지적을 구현했다. 초기 구현은 Claude, 수정분 검토는 Claude가 맡는다. PR #10을 유지하며 병합하지 않는다.
- 요청 전략의 위험 입력 누락 시 오류로 중단하고 배분 0은 독립적으로 정지한다. 지정가를 크기·손절·강제청산·예약의 기준으로 통일했다. UoW 대기 뒤 위험 상태를 재검사한다.
- 재개는 평가액·두 최고점·정지 변경 버전이 확인 요청과 같아야 한다. 변경 시 새 확인이 필요하며 성공하면 기존 프롬프트를 모두 무효화한다. 위험 기준은 확인 당시 현재 평가액이고, 적용 검증 후에 정지를 해제한다.
- start_day에 경계 스냅샷에 포함된 입출금을 추가해 최고점을 보정한다. seen_flows로 중복 제거하고 새날 기준에 포함된 금액은 net_deposit에 다시 넣지 않는다.
- 설계 02, M1-03 명세, README에 연결 계약을 보완했다. 기존 작성자의 HANDOFF 기록은 보존했다.
- 회귀 테스트 21개 추가. Python 3.12.14·3.14.3에서 각각 pytest 312 passed 및 28 subtests passed, ruff check/format, mypy strict(39 소스) 통과. 다음 담당자: Claude Code가 수정분 재검토.


## 2026-10-20 — Codex (PR #10 Claude 재개 확인 P2 수정)

- 사용자 요청에 따라 평가액의 완전 일치 조건을 필수 설정 `RiskLimits.resume_equity_tolerance_ratio`로 대체했다. 기본값 없이 Decimal 0 이상 1 미만을 받으며 요청 평가액 대비 절대 변동률의 경계까지 허용한다. 설정 로더는 기존 계획대로 M3이고, 지금은 RiskLimits로 주입한다.
- pause_revision·record_peak 일치 검사를 유지한다. 허용 폭 초과 시 새 확인을 요구하며 정지를 유지한다. 허용 폭은 ResumePrompt에도 포함한다.
- reset_risk_peak(at, actor)는 이전 화면 값을 받지 않고 현재 평가액으로 설정한다. ResumeRecord.peak_reset.new_risk_peak에 실제 적용값을 남긴다. 기록용 최고점은 보존한다.
- 포지션 미실현 손익 소폭 상승·하락과 경계, 큰 하락·경계 초과, 이전 정지 버전, 기록용 최고점 변경 및 필수 설정 검증 등 14개 회귀 사례 추가.
- Python 3.12.14·3.14.3 각각 326 passed 및 28 subtests passed, ruff check/format, mypy strict(39 소스) 통과. 설계 02·명세·README·설정 예시를 갱신했다. 다음 담당자: Claude Code 재검토. PR은 병합하지 않는다.

## 2026-10-20 — Claude Code (PR #10 병합, 설계 정리)

- PR #10(M1-03 위험 엔진) 병합(1855a78). 구현 Claude → Codex 검토·수정(c099a6e, 7a7d996) → Claude 재검토.
- 02 끝에 임시로 붙어 있던 "PR #10 구현 계약 보완"을 설계 결정 D82~D85로 정리해 02의 해당 절(2장 공통 규칙, 3장, 5.1절, 1장)에 넣음. Q13에 재개 확인 허용 폭 추가.
- 검증: 문서 변경만. 다음: M1-04(포트폴리오·보호 주문), M1-05(가상 거래소) 병행 가능.
- 추가: 사용자 결정으로 역할 고정과 예외 규칙(D86)을 WORKING_RULES·05에 기록. 다음 배정: M1-04 → M1-05는 Codex, Claude는 병행으로 M2-01(해석 목록 먼저 확인받은 뒤 구현, 검토는 Codex).

## 2026-10-20 — Claude Code (V1 기준 확정, 문서 PR 병합)

- 사용자 승인으로 문서 PR #11(D82~D86), #13(D87) 병합.
- M2-01로 받은 3년 데이터(저장소 밖)로 변동성·손절 거리·강제청산 여유·펀딩 통계를 내고, 사용자가 V1 연구 프로필과 통과 기준을 결정 → D88(04 3.2절). 설정 예시에 `[backtest.research_risk]`, `[backtest.selection]` 추가.
- 주의: 수수료 값은 M2-03 구현 때 Bybit 공식 수수료표로 다시 확인.
- 남은 미정: Q13(실거래 수치, V2 이후).


## 2026-10-21 — Codex (PR #12 검토 및 M1-04 구현)

- PR #12의 0db79c1을 M2-01 명세/I1~I14와 대조했다. 리뷰 5592002558에 P2 3건: 펀딩 내부 누락 재수집 없음, 정확한 다음 펀딩 경계 건너뜀, 응답 충돌 때 series 일부 저장. Python 3.14 기존 349개/28 subtests 통과 및 각 결함 재현. Claude 수정 후 재검토 필요하며 병합하지 않았다.
- 최신 main(53a5085)에서 `task/M1-04-portfolio-and-protection` 구현. 시장 신호·공유 AI 결과, 계좌별 장부·펀딩 배분, 에피소드 퇴장, 보호 요청, 순수 청산 상태 기계와 outbox를 추가했다. M1-03 게이트의 동일 UoW 훅으로 결정/주문/청산 의도를 원자적으로 연결했다.
- 손절 부착은 EntryRequest 생성과 전송 payload 변환에서 확인한다. 보호·청산은 reduce-only이며 실제 전송은 하지 않는다. UNKNOWN 청산 주문은 전량 예약하며 IOC 종결 뒤에만 남은 수량을 재시도한다.
- 47개 테스트 추가: 신호 중복/AI 실패·축소/계좌 격리/부호표/부착 손절 선후 역전/늦은 진입/종결 조건/다른 전략 보호/재시도/재시작/외부·초과 감소/펀딩 중복/롤백 등. Python 3.12.14·3.14.3 각각 전체 373 passed + 28 subtests, ruff check/format, mypy 43 소스 통과.
- 저장소·실제 전송·저널 연결 책임과 전체 stop snapshot 확인 계약을 M1-04 명세와 README에 적었다. 다음 담당자는 Claude Code 구현 검토, 사용자 병합 후 M1-05.
- M2-03 시작 시 D88의 `[backtest.research_risk]`·`[backtest.selection]` 설정 키와 Bybit 공식 수수료 값을 확인한다. 불편/모호한 점은 `[Codex]` PR 코멘트로 남긴다. M2-03 명세에도 이 체크를 추가했다.


## 2026-10-21 — Codex (PR #16 Claude 검토 P1 2건·P2 1건 수정)

- 일반 보호는 체결 수신 시각/stop revision 이후 시작한 전체 손절 조회가 있어야 발행한다. 체결→부착 손절 역순 도착 시 중복 PROTECT를 만들지 않으며, 체결 전에 시작한 조회는 거절한다.
- `ProtectionRecovery`를 계좌 UoW 저장소에 추가했다. 일반 보호에도 `ClosePolicy` 횟수·간격을 적용하고 재시작/롤백에 상태를 보존한다. CREATED/SUBMITTED/UNKNOWN은 중복 발행하지 않는다. 한도/미확정 시간 초과 시 계약 정지·알림과 미보호 잔량만 축소한다. 기존 손절은 유지한다.
- `recover_system_pause`는 정지 후 새 손절 조회와 같은 동기화 회차의 거래소 포지션을 검사한다. 미확정 주문·미반영 체결·귀속 보류·미완료 청산·미인수 작업·수량 불일치가 있으면 정지를 유지한다. 성공은 감사 알림을 남기고 포트폴리오 계약 정지만 해제한다. 수동/HALTED는 그대로다.
- M1-04/README 호출 계약과 M1-06 영속화·조회 시작 시각·정지 해제 연결을 보완했다. P3의 조회 누락 손절 실제 종결 상태는 M1-06/M3에서 주문/체결 이력으로 대조하도록 기록했다. 레이어 의존성 정리는 이번 수정 범위에 포함하지 않았다.
- 회귀 사례 14개 추가 및 기존 보호 테스트의 조회 장벽 반영. Python 3.12.14·3.14.3 각각 387 passed + 28 subtests, ruff check/format 및 mypy strict(43 소스) 통과.
- 다음 담당자: Claude Code가 PR #16 수정분 재검토. 병합하지 않는다. M2-03 D88 설정 키/공식 수수료 확인 인수인계는 그대로 유지한다.
