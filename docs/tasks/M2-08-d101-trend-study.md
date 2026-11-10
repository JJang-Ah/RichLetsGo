# M2-08 D101 1단계: S1-B 분산 추세 신호 연구

- 상태: 08a 완료(PR #52 303ef3d, Claude 재검토 통과). 08b 사전 등록 게시(PR #53 58f946e). **08c 연구 1회 실행 결과 NO_GO**(PR #54 b0ddc69) → 다음 가설은 D102(M2-09)
- 구현: Codex / 검토: Claude Code (D86, D100)
- 근거: D101, **04 3.4가 계약 원문**이다. 이 명세는 그 계약을 도구·파일·테스트로 옮긴 것이고, 둘이 다르면 04가 우선한다(다르면 검토에서 지적).
- 브랜치·PR (순서대로 한 PR씩)
  - `task/M2-08a-trend-study`: 연구 도구 + 합성 검증 (Codex)
  - `docs/m2-08b-preregistration`: 연구 사전 등록 + 실행 절차 게시 (Claude, D100 4번; Codex 검토 뒤 병합)
  - `task/M2-08c-result`: 등록된 연구 1회 실행 + 결과 게시 (Codex)

## 목표

D98 S2-M은 1단계 NO_GO(PR #50)로 끝났다. D101은 S1-B 일봉 채널 돌파를 설계용 풀의 유동성 있는 모든 계약에 독립 적용해서, 우위가 소수 거래가 아니라 넓게 나오는지 확인한다. 결과는 04 3.4 5의 진행 기준(go/no-go) 하나다. 판정이 아니라 설계 참고다.

## 지켜야 할 원칙

1. **새 수집 없음.** D98 설계용 풀 DB(data_hash `42acf4dfb41f5d9eff49c02c73a37cc5bc8d6c3d48de04b42382d0b04a08bc6c`)만 읽는다. 판정용 풀 심볼의 데이터는 요청·조회하지 않는다. 네트워크를 쓰는 코드는 없다.
2. **DB는 읽기 전용.** SQLite를 `mode=ro` URI로 연다. D98 freeze 기록을 바꾸거나 다시 freeze하지 않는다.
3. 연구 조건(격자·고정값·비용·진행 기준)은 사전 등록으로 먼저 게시하고 연구는 한 번만 실행한다. 결과를 본 뒤 하나라도 바꾸면 새 가설이다.
4. 개발·테스트는 합성 데이터로만 한다. **실제 DB로 시험 실행하지 않는다.**
5. **D98 결과 재현성 유지.** `research/d98/`의 동작·출력을 바꾸지 않는다. 공통 함수가 필요하면 import해서 쓰고, 고쳐야 하면 D98 회귀 테스트(합성 digest 고정)를 함께 둔다.

## 만들 것 (08a)

- 새 패키지 `src/richletsgo/research/d101/`, CLI `tools/d101_study.py`.
- **재사용** (import, 동작 변경 없음)
  - `research/d98`: 풀 파일 검증, 자산 키·동시 활성 계약 선택, 데이터 수명·폐지(`E_a`) 처리, 범위 판정 함수, data_hash 계산(`audit`), 유동성 등급(D99), 펀딩 반영식, 결과 통계(평균·trim·연도 기준), checkout 검증·등록 게시·one-shot 잠금·상태(`execution`, `locks`), LF 출력.
  - `backtest.codec`의 `encode`·`digest`.
- **새로 만들 것**
  - `daily.py`: 1시간봉 24개 → UTC 일봉(04 3.4 1). 한 개라도 빠지면 그날 일봉 없음.
  - `signals.py`: 04 3.4 2의 돌파 판정(엄격 비교, 신호일 제외), 방향 모드.
  - `simulate.py`: 04 3.4 3의 거래 상태기계. 계약마다 거래 하나, 각 t에서 청산 처리 → 보유 없을 때만 신호, 반대 돌파 뒤집기, 진입봉부터 1시간봉 순서(펀딩 → 00:00 경계에서 reverse/H/추적 손절 → 손절 판정), 갭, H = 2160시간, 결과 무효(누락 시각에서 끝내고 제외, 다음 t부터 재개).
  - `study.py`: 평가 단계 1~6 집계(1~4는 조합 무관, 보유 여부와 관계없이 모든 `(계약, t)`), 조합 4개, 진행 기준 1~5, 기술용 출력(04 3.4 6).
  - `statistics.py`의 **진입일 묶음 평균**(기준 4): 같은 신호 시각 t의 거래 R 평균을 하나로 보고, 그 값들의 평균. 결과 무효 거래는 넣지 않는다.
- **등록·실행 경계** (PR #51 Codex 검토 P2): D98의 `execution.validate_checkout`·`run_registered`는 D98 등록 경로·`POLICY`·D98 연구에 묶여 있으므로 **D101에서 호출하지 않는다**. D101은 자기 등록 스키마(`D101-study-v1`)·검증·실행 오케스트레이션을 `research/d101/execution.py`에 따로 둔다. D98에서 가져오는 것은 공통 primitive뿐이다: Git clean·`HEAD = C`·C가 `origin/main`의 조상·게시 blob 바이트 비교, one-shot 잠금·상태, 원자적 LF 출력, data_hash 계산. 이 primitive가 D98 함수 안에 섞여 있어 그대로 import할 수 없으면, 동작을 바꾸지 않는 추출(공통 모듈로 옮기고 D98은 그것을 호출)만 허용한다. 이때 D98 합성 회귀 digest와 D98 테스트가 그대로 통과해야 한다.
- **DB 확인 순서** (계산 전, 하나라도 실패하면 거절): 풀·감사·등록 파일 digest → checkout 검증(D98과 같은 C/P 규칙) → DB freeze 기록 = D98 등록 digest `f7388c0737eafecee0ead281c8aea486c3155ba9c32303742a53273b3106d2e3` → data_hash 재계산 일치 → 계산.
- **출력**: `backtest_results/d101-study/<등록 digest>/`에 `D101-study.json`(body + execution, body digest), `D101-study.md`, `D101-study-trades.jsonl`(거래마다: symbol, combination, d, t, P_e, P_x, exit_time, exit_reason, R, R₀, funding, slippage, holding_hours, btc_state). 모두 `newline="\n"`.
- **계약별 처리**: D98처럼 계약 단위로 읽고 worker 수는 설정값으로 둔다. 1 worker와 다중 worker의 body digest가 같아야 한다.

## 완료 조건 (08a)

- [x] 일봉 생성: 24개 중 하나 빠지면 그날 없음, OHLC 값, UTC 경계
- [x] 돌파: 엄격 비교(같은 값은 돌파 아님), 신호일 제외, N = 20/55 경계
- [x] 상태기계 합성 회귀
  - 진입 → 추적 손절 상향(롱)/하향(숏) → 손절 청산, 불리한 방향으로 손절이 움직이지 않음
  - 새 손절은 그 봉부터 효력(과거 봉 소급 없음)
  - 반대 돌파 청산과 같은 t의 반대 방향 진입(both), `long` 모드에서는 청산만
  - 보유 중에는 같은 계약 새 신호 없음
  - 갭 진입(진입과 동시 청산, 평균에 포함), 보유 중 시가 갭 손절
  - H = 2160시간 청산 시각 정확히 `t + 90일`의 첫 1시간봉 시가
  - 펀딩: 진입봉 시작 τ 제외, 청산 시각 τ 포함, 1·2·4·8시간 주기
  - 결과 무효: 보유 중 1시간봉 누락, 펀딩 간격 > 8h
  - 폐지: `E_a` 청산(D98 규칙 재사용)
  - `R₀ ≤ 0` 강제 입력 → 결과 무효 `nonpositive_risk`
- [x] 평가 단계 개수: 1~4가 조합·보유 여부와 무관함, 과거 입력 창 `[t − 56일, t)`
- [x] 진행 기준 1~5 경계값(300·0.10·ceil 1%·30개 해 3개·과반 엄격·묶음 평균 0·4개 중 2개)
- [x] DB 읽기 전용: 쓰기 시도 시 오류, freeze 기록 불일치·data_hash 불일치 거절
- [x] D98 합성 회귀 digest 불변
- [x] 합성 벤치마크: 600계약 × 43,000봉 규모에서 경과 시간·최대 메모리, 1 worker = 다중 worker digest
- [x] 판정용 풀 심볼을 입력으로 줄 수 없음(풀 파일의 설계용 계약만 읽음, 테스트)

## 08b 사전 등록 (Claude, 08a 병합 뒤)

- 08a 병합 커밋 C의 깨끗한 worktree에서 등록 파일(`docs/reports/D101-study-preregistration.json`·`.md`)을 생성한다. 등록에는 C, 풀·감사·data_hash, D98 freeze 등록 digest, 격자·고정값·진행 기준 전문, 04 3.4 8의 한계를 넣는다.
- 실행 절차 문서(`docs/reports/D101-study-run-procedure.md`)는 D98 절차를 따르되 PowerShell 명령으로 쓴다. 결과 게시 위치는 `docs/reports/D101-study-result/`.

## 08c 실행 (Codex, 08b 병합 뒤 한 번)

- 절차 문서대로 C worktree 전용 venv에서 1회 실행하고 결과 PR을 올린다. DB 경로는 `C:\J_PROGRAM\RichLetsGo\data\backtest\market_data\d98-design.sqlite`(D98과 같음).
- 결과를 본 뒤 조건·격자·코드를 바꾸지 않는다. NO_GO여도 같은 가설을 다시 돌리지 않는다.
