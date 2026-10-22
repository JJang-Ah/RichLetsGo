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

## 2026-10-20 — Claude Code (M2-01 구현, D86 예외)

- Codex 사용 한도 동안 D86 예외로 Claude가 구현. 해석 목록(I1~I14)을 먼저 올려 사용자 확인 후 구현. 검토는 Codex.
- `marketdata/`: models(Bar, FundingRate, InstrumentSnapshot, RiskLimitTier), validate(진행 중 봉 제거, OHLC·정렬 검사, 빈 구간·D87 펀딩 간격), store(SQLite, Decimal TEXT, 같은 키 다른 값이면 배치 전체 롤백), bybit_public(urllib, 재시도·백오프, kline 1000개 페이지, 펀딩 endTime 역방향 페이지, risk-limit cursor), ingest(빈 구간만 요청, 실행 기록). 설정 예시에 `bybit_public.base_url`, `paths.backtest.market_data_dir` 추가.
- 구현 중 실제 응답으로 정정한 것: I7 증거금률은 퍼센트가 아니라 **비율**(1단계 MMR 0.0033), 최고 단계 IMR은 정확히 1(100%, 1배)이라 범위를 (0, 1]로.
- 검증: Python 3.14.3에서 ruff·format·mypy(44 소스)·pytest 349 passed(신규 23, 실제 응답 fixture 4개, 네트워크 접근 차단). 실제 3년 수집 1회: 두 계약 각각 일봉 1,094 / 1시간봉 26,279 / 펀딩 3,285, 빈 구간·거절·충돌 0, 41초. 재실행 2초, 봉·펀딩 요청 0.
- 다음: Codex 검토. M2-02는 M1-04 이후.

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

## 2026-10-20 — Claude Code (M3 작업 명세)

- Codex 대기 중 M3(데모 연결) 명세 8개 작성: `docs/tasks/M3-00`~`M3-07` (설정·자격 증명, Oracle 저장소, Bybit 어댑터, Trader 조립, 텔레그램, Watchdog·서비스, 데모 1주 운영).
- Bybit 데모 공식 문서로 확인: 공개 시세 WS는 데모에 없음(메인넷 사용), WS 주문 미지원, 데모 주문 기록 7일 보관, 서명 시각 범위. M3-00 표에 정리.
- 제안 의존성: `aiohttp`, `oracledb`, `keyring`. 사용자 결정으로 Q22 → D89(Windows 서비스는 NSSM). 남은 질문 Q23(Oracle 설치 시점).
- 검증: 문서 변경만. 다음: Codex 명세 검토(PR #15). 구현은 M1-04~06, M2-02 병합 뒤.

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


## 2026-10-21 — Codex (PR #16 재검토: 미보호 축소 재시도)

- Claude 재검토 P2를 수정했다. `ProtectionRecovery.reduction_attempts/last_reduction_at`을 저장하고 축소 액션 키에 횟수를 넣는다. `retry_after` 간격과 `max_close_attempts`를 적용하며 열린 CLOSE 잔량·미반영 누적 체결을 계속 제외한다.
- 재시도 한도 소진 뒤 추가 주문은 만들지 않고 계약 정지를 유지하며 수동 점검 알림을 한 번 남긴다. 기존 손절은 유지한다. 축소가 시작되면 보호 UNKNOWN이 뒤늦게 종결되어도 축소 복구 경로를 유지한다.
- P3 선택 사항: 보호 재시도마다 새 조회를 강제하지 않는 기존 동작을 유지한다. 종결 확인된 보호만 재시도하고, 새 진입 체결은 계속 조회 장벽을 갱신한다.
- 회귀 6개: IOC 무체결 만료/거절, 재시작 후 간격·횟수 보존, 한도 소진, UNKNOWN/OPEN 예약, 종결 주문의 미반영 체결 제외. Python 3.14 전체 393 passed + 28 subtests, ruff·mypy 통과. Python 3.12 및 CI 결과는 PR 코멘트로 남긴다.
- 다음: Claude 수정분 재검토. 병합은 사용자 담당.

## 2026-10-21 — Claude Code (PR #15 Codex 검토 반영)

- P1 커밋 결과 불명(M3-02): COMMIT 전 장애(롤백)와 COMMIT 응답 유실(결과 불명)을 나눔. `uow_commits` 표지 행 + 재연결 후 같은 계좌 잠금으로 판정, 판정 전 새 ID·전송 금지, 진입 정지. 장애 지점별 테스트를 완료 조건에 넣음.
- P2 동기 저장소 ↔ 비동기 Oracle(M3-02): 잠금 → 작업 집합 적재 → 동기 조작 → 커밋 때 바뀐 행만 반영. 작업 집합 정의(N일, 기본 7일)를 메모리 구현에도 적용, N일보다 오래된 봉 신호는 게이트 거절. 잠금 안 대기 금지는 외부 호출로 한정.
- P2 경계 처리(M3-04): 경계 T별 필수 입력 장벽, 대기 기한 후 REST 보충, 계약별 신호 생략, 펀딩은 계좌 기록 기준·늦으면 알림 후 진행, 단계 체크포인트, 자정 봉 순열 테스트.
- P2 전략 설정(M3-01·04·07): `[strategy.S1/S2]` 필수, `source`(m2-04 보고서 해시 / manual). 데모 1주 판정은 M2-04 기반 설정만. 인프라 작업은 먼저 시작 가능.
- 그 밖: UTA 격리 확인은 `/v5/account/info.marginMode`(M3-03), keyring은 서비스 실행 계정으로도 확인(M3-01), M1-04 outbox·보호 복구·정지 해제 계약을 M3-02·04가 인수.
- 검증: 문서 변경만. 다음: Codex 재검토.

## 2026-10-21 — Claude Code (PR #15 Codex 재검토 P2 2건 반영)

- 늦은 펀딩(M3-04): ①단계에서 경계 T 기준 전략별 보유량 스냅샷을 남기고, 펀딩은 도착 시점이 아니라 정산 시각 스냅샷으로 배분. 스냅샷 0이면 미배분 기록·알림. 거래일은 정산 시각 기준. 자정 정산이 늦으면 `start_day`와 신규 진입을 보류하고 손절·청산은 계속. 선행 변경: M1-04 `funding`에 `settled_at` 추가.
- 단계 복원(M3-04): 단계마다 상태·출력·체크포인트를 같은 UoW로 커밋. 입력 봉·스냅샷·`start_day`·신호·시장 입력을 저장해 재시작 때 다시 계산하지 않음. 전송 단계는 M1-06 복구 규칙. 장애 지점(커밋 전/후) 테스트 추가. 테이블은 M3-02 스키마에 추가.
- PR #12는 Codex 재검토 통과(fa56555), 사용자 병합 대기.
- 검증: 문서 변경만. 다음: Codex 재검토.

## 2026-10-21 — Claude Code (PR #12 Codex 검토 P2 3건 수정)

- main 병합(README·설정 예시·HANDOFF 충돌은 양쪽 내용 유지).
- 저장 원자성(I10): 봉·펀딩 모두 한 계열의 응답을 전부 받아 검사한 뒤 한 번에 저장. 응답 충돌·요청 실패 시 그 계열은 저장 0건.
- 펀딩 재수집(I11, D87): 저장 범위 양끝은 한 주기 이하여도 항상 다시 조회하고, 내부 빈 구간(기록 간격 > 수집 시점 주기)도 요청 범위에 넣음.
- 회귀 테스트 5개 추가(수정 전 코드에서 5개 모두 실패 확인), 충돌 테스트 기대값 변경. Python 3.14: 421 passed + 28 subtests, ruff check/format, mypy strict(48 소스) 통과. 3.12는 CI로 확인.
- 남은 것: I13 보완 권장(거절 원문·사유 저장)은 이번에 하지 않음. 다음: Codex 재검토.
- 추가: Codex 권장 I13 반영. `ingest_runs.summary_json`을 `{"request": 원래 요청, "series": 계열별 결과}`로 바꾸고, 계열마다 거절 행의 원문·사유·요청 범위를 최대 200행 남김(개수는 전부). 테스트 2개 추가 + CLI 테스트 확인 보강, 423 passed.

## 2026-10-21 — Claude Code (PR #15 Codex 3차 검토 반영)

- P1(M3-04 ⑤): 지난 수정에서 ⑤를 ④의 저장 평가액으로 고정한 것은 잘못이었다. 결정이 없는 신호는 잠금 뒤 최신 평가액·정지·보유·예약·시세 신선도로 판단하고, 쓴 기준을 결정과 함께 저장. 이미 결정된 신호만 그대로 복원. "중단 없는 실행과 같음"은 외부 입력 이력이 같을 때로 한정.
- P2(M3-04 ③): ③을 준비(계좌 스냅샷·입출금 참조를 불변 레코드로 먼저 커밋)와 반영(`start_day`·정지·체크포인트)으로 나눔. 장애 지점별 테스트 추가.
- main 병합(PR #12 병합분, HANDOFF 충돌 정리). 검증: 문서 변경만. 다음: Codex 재검토.


## 2026-10-21 — Codex (M1-05 가상 거래소 구현)

- main c7ee581(PR #15 병합)에서 `task/M1-05-simulated-exchange`를 시작했다. 비동기 ExchangeGateway 계약, OMS 요청 변환, 구조화된 명령/조회 오류, 결정적인 가상 거래소를 추가했다.
- 다음 봉 시장가·엄격 지정가·갭/진입 봉 손절·손절 우선, 체결별 부착 손절, reduce-only, 손절 합계 조정 3종, 지갑/미실현 손익/펀딩을 구현했다. 모든 가격·수량은 Decimal이고 시계·상품 규칙·비용을 주입한다.
- 선언형 FaultPlan으로 응답 유실·조회 숨김/오류·순서 역전/중복·취소 경합·스트림 끊김·부착 손절 연결 지연·IOC 부분 체결/거절·취소 오류를 재현한다. 실제 어댑터는 추가하지 않았다.
- 52개 테스트 추가. Python 3.12·3.14 각각 전체 475 passed + 28 subtests, ruff check/format, mypy strict 51 소스 통과. 테스트는 실주문·네트워크·개발 PC 경로에 의존하지 않는다.
- M1-05 명세에 입력 순서, 펀딩/자정 경계, 손절 조정 가정, 합성 시각, stream/REST 복구와 fault 해석을 기록했다. 다음: Claude 구현 검토, 사용자 병합 뒤 M1-06. M2-03 시작 시 D88 설정 구조·공식 수수료 확인은 계속 필요하다.


## 2026-10-21 — Codex (PR #17 Claude P2 수정)

- 미발동 부착 손절의 연결 이벤트를 나중에 전달할 수 있도록 `SimulatedExchange.release_held()`를 추가했다. 보류된 원래 이벤트를 생성 순서대로 한 번만 방출하며 REST 주문·체결·잔고는 바꾸지 않는다. 호출하지 않으면 기존 D61(손절 execution 뒤 연결) 경로를 유지한다.
- 회귀 4개: 미발동 손절의 지연 연결/수동 해제 후 체결, 자동 해제 후 중복 방지, 끊김 중 방출 유실과 REST 보존, 다중 손절 순서. Python 3.12·3.14 각각 479 passed + 28 subtests, ruff check/format, mypy strict(51 소스) 통과.
- P3는 M1-05 명세에 인수인계했다. trigger 방향 거절 기본 모델은 M3 C3 확인 후 판단하며, 지정가 갭의 가격 개선을 모델링하지 않는 한계는 M2 보고서에 반영한다.
- 다음: Claude PR #17 수정분 재검토. 병합하지 않는다.

## 2026-10-21 — Claude Code (M1-06 명세 보완)

- PR #17(M1-05) 병합(606669b). M1-06 시작 전에 M1-04·05 병합으로 생긴 연결 계약을 명세에 정리: outbox 작업 실행표(작업별 호출·저널 여부·결과 반영), `QUERY_STOPS` 조회 절차, 늦은 부착 손절 연결 회귀 테스트, 커밋 예외 시 미전송.
- 빈틈 발견: M1-04 `TRIM_STOP`(남길 수량)을 실행할 게이트웨이 수단이 없음 → 제안 D90(`amend_stop`에 수량 추가, 안 되면 새 보호 먼저·기존 취소 나중). Bybit 문서상 조건부 주문 수량 변경은 가능, 부착 Partial 손절은 미확인 → M3-07 C10.
- 검증: 문서 변경만. 다음: Codex 검토, 사용자 확인 후 Codex가 M1-06 구현.
- 추가(PR #18 Codex 검토 P2 3건 반영): D90 수량 변환(남길 잔량 → 전체 수량)과 저널 amend 기록·재생 판정(과거 목표 재전송 금지, 03 5.4절 갱신), 대체 절차의 전체 조회 확인·분기·복합 작업 저널, 커밋 예외 분기표(진입은 항상 미전송, 보호는 저널 경로).
- 추가(PR #18 Codex 재검토 P2 3건): 복합 작업은 `group_id`로 묶고 요청마다 새 `operation_id`(저널 키 충돌 해소, 03 5.4절·M3-02 열 추가). ③ 취소 조건을 "취소 대상 잔량을 빼고도 모든 전략 보호 ≥ 보유"로 바꾸고 이미 목표면 종료. 결과 불명 보호 예외를 M3-02에 반영(판정 뒤 저널을 먼저 합치고 outbox acknowledge 후 전송기 재개, amend는 재실행 금지).
- 추가: Codex 재검토 통과, 사용자가 D90 확정(2026-10-21). PR #18 병합 후 Codex가 M1-06 구현.


## 2026-10-22 — Codex (M1-06 동기화·저널·V0 통합)

- PR #18 병합 명세에서 `task/M1-06-sync-journal-v0` 구현. 스트림 선구독·이벤트 버퍼·REST 이력/미체결/체결/포지션 대조, 안정화 전 진입 정지, CREATED 종료·SUBMITTED UNKNOWN, 저널 인수 뒤 보호/청산 outbox 재개를 연결했다.
- ProtectiveJournal은 주입 Path 아래 JSONL requested/result를 각각 fsync한다. 꼬리 격리·중간 손상 차단·구간/보관·전역 seq, DB 실패 보호 전송·저널 실패 긴급 전송·항상 거래소 대조를 구현했다. 진입은 SUBMITTED 커밋 실패 시 전송하지 않는다.
- D90: qty-only amend와 남길 잔량→부호 있는 전체 수량 변환, UNSUPPORTED fault, 신규 보호 먼저/전체 조회/기존 취소/재조회 교체 절차. 그룹별 진행·생성/취소 재시도와 요청별 ID를 저장한다. 일반 TRIM이 교체용 새 손절을 취소하지 않게 그룹이 둘 다 관리한다.
- `RecoveryRepository`에 정지·저널·UNKNOWN/미반영 체결 타이머·교체 상태를 트랜잭션으로 저장한다. Oracle 구현과 commit 결과 불명 판정은 M3-02가 인수한다. ID 생성기는 모든 주문 생산자와 재시작을 포함해 고유해야 한다.
- V0 통합 61개와 39행 대응표 추가. DB/디스크 실패, 늦은/중복/역순 체결, 동시 계좌 한도, 손절·청산 경합, D64 늦은 체결, D90 자동 축소/부족/UNKNOWN/소진 및 생성·취소 단계 재시작을 검증했다. 외부 연동은 M3/M4로 구분했다.
- D39: trading_lab 3파일·기존 테스트 9개 삭제, 임시 pytest pythonpath/ruff 제외 제거. README·REVIEW에 새 구현 및 초기 발견 1~8 대응을 기록했다.
- 검증: Python 3.12·3.14 각각 531 passed. ruff check/format, mypy strict(54 소스) 통과. 실제 주문·Oracle·텔레그램은 실행하지 않았다.
- 다음: Claude 구현 검토, 사용자 병합. 실제 Bybit 부착 Partial 수량 변경·자동 축소는 M3-07 C10. M2-03 시작 시 D88 설정 키 구조와 공식 수수료 확인은 계속 필요하다.


## 2026-10-22 — Codex (PR #19 Claude P1·P3 반영)

- 체결 시각 대신 계약별 완전 대조 시작 시각을 recovery에 저장한다. history/executions/open 조회·반영 성공 뒤에만 전진하며, 미확정 주문·미반영/귀속 보류 체결이 남으면 기준을 유지한다. 계좌 조회는 가장 오래된 계약 기준을 사용한다.
- 실제 대조 공백이 조회 범위를 넘으면 history_gap을 영속화하고 진입 정지를 유지한다. 현재 조회 가능한 범위의 REST·UNKNOWN·보호 outbox·교체 처리는 tick과 synchronize 모두 계속한다. 제한된 조회 성공만으로 공백을 지우지 않는다.
- 03 4.3절·M1-06 명세를 먼저 갱신했다. P3: exhausted 교체 그룹 수동 종료·재대조는 M3-05에 인수인계했고, missing_action_order 알림에 수동 확인 필요를 명시했다.
- 회귀 4개: 무체결 8일간 일일 대조 후 재시작·청산, 실제 8일 중단 시 정지 유지와 청산 전송, 조회 실패 시 워터마크 유지·조회 시작 시각 저장, 계약별 가장 이른 워터마크. 기존 늦은 체결 회귀도 통과.
- Python 3.12·3.14 각각 535 passed. ruff check/format, mypy strict(54 소스) 통과. 다음: Claude 재검토, 병합은 사용자 담당.

## 2026-10-22 — Claude Code (M1 완료 판정, M2-02 명세 점검)

- PR #19(M1-06) 병합(06abde8)으로 M1 작업 6개 완료. M1-00에 완료 판정 표 추가: V0 39행 추적(대응표 테스트 42개 실재 확인), CI·로컬 535 passed, `trading_lab` 삭제, 외부 연결 없음 → **M1 완료**.
- M2-02 명세를 병합된 코드와 대조해 보완(사용자 확인 2026-10-22): `MarketSignal` 확정과 `EXIT` 가격 필드 규칙, S1·S2 입력·상태 계약(직전 상태, 열린 S2 에피소드, JSON 왕복), **추적 손절 연결 경로 추가**(M1-04에 trigger 수정 작업이 없던 빈틈 → `trail_stop` + `AMEND_STOP` + 동기화기 연결, 전략 코드와 커밋 분리).
- 다음 담당자: Codex — M2-02 구현(`task/M2-02-indicators-and-strategies`). 검토: Claude Code.
- 추가(PR #20 Codex 검토 P1 3·P2 1 반영): 추적 손절 연결을 별도 작업 **M2-02a**로 분리하고 먼저 구현하기로 함. 목표 갱신과 주문별 맞추기 분리(같은 목표 재시도·늦은 OPEN), 진행 중 amend 판정 후 재시도·`TrailRecovery` 한도, 전송 직전 확인(낮추지 않음), trigger만 바꾼 `UNSUPPORTED`를 D90 교체에서 제외, 늦은 진입 체결이 목표를 되돌리지 않게 `stop_episode_id`, `InstrumentSpec` 입력. M1 완료 표 문구 정정. 순서: M2-02a → M2-02.
- 추가(PR #20 Codex 재검토 P2 2건): M2-02a `attempts`는 실제 보낸 요청 수(거절·TIMEOUT·Accepted 모두 계수, 전송 전 폐기는 제외)이고 작업 키에 다음 시도 번호를 넣어 재시도가 옛 키에 막히지 않게 함. Accepted도 적용 확인 전에는 `in_flight`. `trail_stop`에 대상 `symbol`을 따로 받아 `spec.symbol`과 비교.



## 2026-10-22 — Codex (M2-02a 추적 손절 연결)

- PR #20 병합 명세에 따라 `task/M2-02a-trailing-stop`에서 별도 구현했다. `trail_stop`은 계약 규칙 검증·방향별 tick 정렬 후 목표만 갱신하고, reconcile이 확인된 주문별 `AMEND_STOP`을 만든다. 같은 목표 재시도·늦은 OPEN·추가 부분 체결도 포함한다.
- `TrailRecovery`를 계좌 recovery 저장소에 저장하고 requested 저널 인수와 함께 시도 수·시각·진행 중 operation_id를 기록한다. Accepted는 적용 확인 전까지 진행 중이며, 간격 이후 성공한 조회로 미적용/대체를 판정한다. 작업 키에 다음 시도 번호를 넣고 재생을 멱등 처리한다. DB 실패 중 전송은 디스크 저널에서 복구한다.
- 전송 직전 현재 목표·주문·청산 상태를 재확인한다. 한도 소진 시 계약 신규 진입을 정지하고 기존 손절을 유지한다. trigger-only UNSUPPORTED는 D90 수량 교체에서 제외한다.
- `stop_episode_id`로 같은 에피소드의 늦은 진입이 목표를 되돌리지 않게 하고 새 에피소드는 초기 손절로 시작한다. 자식 손절 알림이 체결보다 먼저 올 때 예약된 TRIM이 새 보호를 없애지 않도록 전송 직전 필요 잔량을 재확인한다. 기존 수량 변환 테스트는 실제 초과 보호 상태를 구성하도록 보정했다.
- 새 V0 통합 19개. Python 3.12·3.14 각각 **554 passed**, ruff check/format, mypy strict(55 소스) 통과. 실제 Bybit·Oracle 호출 없음.
- 다음: Claude가 M2-02a를 검토하고 사용자 병합 후 Codex가 `task/M2-02-indicators-and-strategies`에서 M2-02 구현. 실제 부착 손절 trigger amend와 부모 연결은 M3-07 C3. M2-03 시작 시 D88 설정 예시 키(`[backtest.research_risk]`, `[backtest.selection]`)·수수료 값 확인을 유지한다.


## 2026-10-23 — Codex (PR #21 Claude P2 수정)

- 전송 전 폐기한 AMEND_STOP의 키가 emitted에 남아 같은 목표를 다시 만들지 못하는 문제를 수정했다. TrailRecovery에 실제 시도와 별개인 `discards`를 저장하고 작업 키에 포함한다. 폐기 번호 증가와 acknowledge는 같은 UoW에서 커밋하며 실제 attempts·last_attempt_at은 바꾸지 않는다. 이전 직렬화에는 discards=0을 적용한다.
- 다른 전략의 같은 계약 청산 때문에 폐기 → 청산 종료 → 같은 목표 재전송을 재시작 유무 2개 사례로 검증했다. 중복 reconcile은 작업 1개, 폐기는 시도 0회, 이후 전송은 시도 1회임을 확인했다. 기존 낡은 목표·청산·이미 개선된 손절 폐기 테스트도 유지했다.
- Python 3.12·3.14 각각 **556 passed**, ruff check/format, mypy strict(55 소스), diff check 통과. 다음: Claude 재검토. 병합은 하지 않았다.


## 2026-10-23 — Codex (M2-02 지표·전략 후보 4개)

- PR #21 병합 main(b054f31)에서 `task/M2-02-indicators-and-strategies` 시작. strategy에 ClosedBars, SMA/채널/모표준편차 볼린저/Wilder RSI·ATR, 불변 매개변수·상태·결과, S1-A/B·S2-A/B 순수 함수를 구현했다.
- ClosedBars는 명시적인 UTC as_of로 미마감·혼합·중복·역순·누락 봉을 거절한다. through로 과거 접두 구간을 먼저 선택한다. S1 이전 상태와 S2가 받는 일봉 상태의 시간 경계도 검증한다. Decimal 34자리 지역 컨텍스트를 사용하며 float 변환은 없다.
- S1은 매일 같은 추세의 시장 신호와 단조 추적 목표를 출력한다. 방향 전환은 반대 ENTRY와 새 방향 초기 채널이다. S2는 각 봉의 밴드·RSI로 재진입을 판단하고, 실제 열린 에피소드에만 EXIT를 낸다. 추세 소멸/전환·보유 초과 청산은 지표 워밍업에 막히지 않는다.
- 계산 규약(ATR 첫 TR/RSI flat=50/보유 봉 경계/비양수 초기 손절 시 진입 없음)을 M2-02 명세에 기록했다. S1State·S2Episode·결과는 JSON 왕복 가능하다. Wilder 값 재현을 위해 동일한 데이터 시작점을 유지해야 한다. M2-03은 같은 시각 일봉 먼저, 시간봉 다음 순서와 실제 진입 봉 정보를 연결한다.
- 새 테스트 51개: 손계산 지표, 워밍업, 4후보 미래 봉 추가 불변, 숏 대칭, 밴드 시점 fixture, 최대 보유 경계, 가격 필드, JSON·결정성, 입력 검증, M2-02a trail_stop 연결. Python 3.12·3.14 각각 **607 passed**, ruff check/format, mypy strict(59 소스) 통과.
- 다음: Claude 구현 검토 후 병합. M2-03 시작 시 D88 `[backtest.research_risk]`·`[backtest.selection]` 설정 예시 구조와 수수료 값을 확인하고 불편한 점은 PR 코멘트로 알린다. 실제 주문·성과 측정은 이번 범위에 포함하지 않았다.
