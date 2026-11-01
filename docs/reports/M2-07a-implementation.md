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

## 검증

신규 합성 회귀 41개: 엔드포인트/파라미터 차단, 두 상태 페이지 순회·cursor loop·중복,
실제 관측한 혼합 상태, 정규화·배수·숫자 예외·별칭·제외·관측 자산 강제 DESIGN,
계약 수명과 우선순위, 순서와 무관한 digest, review에서 assignment 호출 금지,
한글 임시 경로/cwd, 미확정 표·미병합 표 차단, 실패 표식·결과 재실행 차단,
정식 결과/요약 생성, 잘못된 수명·심볼·시각 검증. 실제 판정 풀을 테스트에 사용하지 않았다.
대규모 시세 저장/연구 벤치마크는 명세대로 07b/07c 범위다.
