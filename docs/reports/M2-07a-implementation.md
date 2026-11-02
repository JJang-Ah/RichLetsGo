# M2-07a 구현 및 메타데이터 검토

## 범위와 현재 상태

PR #38 병합 `2248b259730da0db230a3654e5e4c96e77bf4879` 기준 구현.
`research/d98`는 기존 Symbol·시세 저장소·실거래 경로를 수정하지 않는다.
메타데이터 `--review`까지 실행했다. 정식 풀 배정·가격 봉·펀딩 이력 수집은 아직 하지 않았다.
`TABLES_FROZEN=False`이며 표 검토가 끝나기 전 정식 CLI는 네트워크 호출 전에 거절한다.

## 도구

```powershell
# 필요한 경우 새 검토 경로를 명시한다. 기존 검토 기록은 덮어쓰지 않는다.
.\.venv\Scripts\python.exe -X utf8 tools/d98_pool.py --review --output docs/reports/D98-metadata-review.json
# 아래는 표 검토/확정 커밋 병합 및 origin/main 갱신 후에만 실행한다.
.\.venv\Scripts\python.exe -X utf8 tools/d98_pool.py --output docs/reports/D98-pool.json
```

CLI 출력 경로는 인자로 받고, git 조회는 실행 모듈의 checkout을 기준으로 한다.
고정된 PC 경로는 없다. 정식 실행은 깨끗한 checkout과 origin/main의 동일 tables.py blob을 요구한다.
`*.started.json`은 실패해도 보존한다. 결과 JSON/요약은 배타 생성하며 덮어쓰지 않는다.
`*.requests.json`에는 요청 시각과 재시도 번호가 남는다. 경로는 instruments-info만 허용하며
category/status/limit 및 중복 파라미터도 검사한다. 가격/펀딩/tickers는 전달 전에 차단한다.

## 실제 메타데이터 검토 기록

- 첫 실행 `870cbcb`에서 Trading/Closed 각 1회 요청 후 중단: Closed 응답에 PendingOpen이 섞여 있었다.
  `D98-metadata-review.started.json`, `.requests.json`을 그대로 보존했다.
- 진단 각 1회: `D98-metadata-diagnostic.json`, `D98-metadata-diagnostic-closed.json`.
  데이터는 명세의 메타데이터 필드만 보존했다. 원래 응답의 가격 관련 필드는 저장하지 않는다.
- 수정 `378b48b`: 응답 상태를 강제로 일치시키지 않고 명세 필터로 제외한다.
- 성공 검토: `D98-metadata-review-v2.json` 및 started/requests. Trading 923, Closed 1045,
  PendingOpen 5, 총 1973개. 실제 총 요청 7회(실패 실행 2 + 진단 2 + 성공 실행 3), 재시도 0.
- 검토 모드는 assignment 함수를 호출하지 않고 해시 배정·풀별 개수를 출력하지 않는다.
  숫자 이름, 정규화/별칭 뒤 동일 키, Closed/Trading 이름 유사도 ≥0.7 쌍을 검토 후보로만 제시한다.
  이름 유사도는 자동 별칭 등록 근거가 아니다.

## 표 초안과 Claude 검토 요청

- 숫자 예외: 메타데이터의 `0G`, `1CAT`, `1INCH`, `2Z`, `4`, `4STOCK`을 보존한다.
  현재 10의 거듭제곱 제거 규칙에도 걸리지 않지만 명시적으로 기록한다.
- 별칭은 명세 초기값 MATIC→POL, RNDR→RENDER, FTM→S를 유지한다.
  동일 키 그룹은 BTT(1000BTT/BTT), POL, RENDER, S다.
  이름 유사 목록의 다른 쌍은 근거 없이 합치지 않았다.
- 제외 표는 M2-06 전체 + USDD + RLUSD. RLUSD는 [발행사 문서](https://docs.ripple.com/products/stablecoin)의 USD 연동 자산 설명에 근거한다.
- [Bybit API 문서](https://bybit-exchange.github.io/docs/v5/market/instrument)에 따라 fundingInterval은 정수 응답도 정규화한다.
- Claude는 숫자 예외/별칭/제외 표를 검토하고, 변경이 있다면 풀 배정을 보기 전에 반영한다.
  특히 1000X 등 배수처럼 보이는 이름과 유사 이름 쌍에 추가 근거가 있는지 확인한다.
  표 승인 후 TABLES_FROZEN을 True로 바꾸는 확정 커밋을 이 PR에 추가하고 병합한다.
  그 이후 정식 1회 실행 → D98-pool JSON/요약 별도 게시·병합 → Q30 확정 뒤 07b 실제 수집 순서다.

## 명세 범위 확인 필요

실제 메타데이터의 AAPLUSDT·NVDAUSDT·TSLAUSDT도 현재 명세의 LinearPerpetual/USDT 필터를 통과한다.
구현에서 임의 제외하지 않았다. 암호자산 전용 풀을 의도했다면 표 확정/배정 전에 설계의 범위 필터를 먼저 보완해야 한다.
이 사실은 PR #39 코멘트로 Claude에게 전달했으며 가격 데이터 조회에 근거한 판단이 아니다.

## 검증

신규 합성 회귀 41개: 엔드포인트/파라미터 차단, 두 상태 페이지 순회·cursor loop·중복,
실제 관측한 혼합 상태, 정규화·배수·숫자 예외·별칭·제외·관측 자산 강제 DESIGN,
계약 수명과 우선순위, 순서와 무관한 digest, review에서 assignment 호출 금지,
한글 임시 경로/cwd, 미확정 표·미병합 표 차단, 실패 표식·결과 재실행 차단,
정식 결과/요약 생성, 잘못된 수명·심볼·시각 검증. 실제 판정 풀을 테스트에 사용하지 않았다.
대규모 시세 저장/연구 벤치마크는 명세대로 07b/07c 범위다.

최종 Python 3.14 전체 836 passed (101.75초), ruff check/format 및 mypy strict 85 소스 통과.
샌드박스 안 첫 전체 검사 프로세스는 출력 없이 정체돼 중단했고, 정상 권한에서 오프라인 전체 검사를 완료했다. Python 3.12는 PR CI 대상이다.


## 2026-11-02 Claude 검토 반영

- PR #40(main db08877) 병합. HANDOFF 양쪽 기록 보존.
- FIELDS에 symbolType/fullName을 추가해 정규화/입력 해시/산출물 해시에 포함. 암호자산은 빈 값·innovation만 허용,
  stock/ETF/commodity/forex는 non_crypto 사유로 제외하고 미지 유형은 중단. SPX 코인은 이름으로 제외하지 않는다.
- ETHBTC 제외. 별칭 13건과 공식 링크를 추가(세부: D98-alias-evidence.md). SKYAI/SKYAI1은 미확정이라 합치지 않음.
- 메타데이터 v3: 실행 코드 411844a, 1973개 응답. 기존 필터의 1130개 중 비암호자산 298개 제외 후 832개.
  이 832개는 스테이블/ETHBTC 제외 표 적용 전 수치이며 풀별 집계가 아니다. 이번 요청 3회·재시도 0.
- 정식 출력은 실행 checkout 기준 docs/reports/D98-pool.json만 허용하며 로컬/원격 main의 started 표식을 검사한다.
  다른 output을 지정해 우회할 수 없다. CLI 인자 경로는 실제 해당 checkout 경로와 비교하므로 다른 cwd에서도 절대 경로로 사용 가능하다.
- requests 감사 파일 쓰기 실패가 원래 예외를 가리지 않도록 add_note로 부가 실패를 남긴다.
- 공개 instruments_page 메서드를 추가해 D98 collect의 비공개 _get 직접 사용을 제거했다.
- 기존 검토 v2/실패/진단 파일은 수정하지 않았다. TABLES_FROZEN=False 유지, 정식 풀 실행 및 가격/펀딩 이력 수집 없음.

- 2026-11-02 최종 검증: Python 3.14 전체 868 passed(58.07초), D98 회귀 73개, ruff check/format 및 mypy strict 85 소스 통과.


## 2026-11-02 동일 이름 분리 방지 최종 반영

- LAYER→SOLAYER, SKYAI1→SKYAI를 추가했다. 새 근거는 v3 fullName 동일 및 Claude의 비대칭 위험 원칙이며 이전 미통합 판단을 대체한다.
- review에 same_fullname_split, build에 사전 배정 차단 추가. 명시적인 DISTINCT_SAME_NAME 예외는 이름+키 쌍 단위로만 적용되고 digest에 포함된다.
- v4 메타데이터 검토: 코드 510dbfa, 1973개 응답, same_fullname_split=[], instruments-info 3회·재시도 0. 실제 풀 배정/가격/펀딩 이력 조회 없음.
- Claude 코멘트 6566546410의 조건을 충족해 TABLES_FROZEN=True로 확정. 이전의 False 상태 기록은 역사 기록이며 현재 상태는 확정이다.
- 다음은 PR 검토/병합 → 정식 풀 1회 실행 및 결과 PR 게시. 병합 전에는 표 blob의 origin/main 일치 가드가 정식 실행을 계속 막는다.

- 최종 검증: Python 3.14 전체 872 passed(58.37초), D98 77 tests, ruff check/format 및 mypy strict 85 소스 통과.
