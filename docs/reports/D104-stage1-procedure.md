# D104 1단계 실행 사전 등록

기준은 #89 병합 e70e47f와 [M2-11 §2~4.1](../tasks/M2-11-d104-tradfi-strategy.md)이다. 코드 C는 `5dd461a5f99d15f64e2791e6566c68074c77ee3c`. [등록 JSON](D104-stage1-registration.json)의 SHA-256은 `f39ff748e38d8d305982ce23545ca4c313716772b0e6adb5a4877876aac4970c`이다. 재수집 등록을 Claude가 검토·병합하면 #92의 증빙으로 전체 설계 구간을 한 번 수집한다. 성공하면 동결한 manifest를 별도 PR에 게시하고, Claude 검토·병합 뒤 한 번 재생한다. 조합은 하나, 초기 현금 10,000 USDT, 용량 E=491, E_slot=232, N_min=100이다. 운영 경로와 독립된 연구다.

## 수집 범위

B=2026-09-01T00:00:00Z(1788220800000 ms)는 배타 끝이다. 등록 파일에 고정한 15종목의 상장 시각과 XNYS/XKRX regular 행에서 완전히 포함된 [O,C) 1,181개를 먼저 열거한다. 워밍업 세션도 가격 입력 무결성 검사에 포함한다. 등록 JSON에 세션표와 일별 요청 계획 각각의 SHA-256이 있다. 이후 단계의 B 인자화(#85 P3-4)는 보류한다.

| 입력 | 허용 요청과 보존 필드 | 완전성 기준 |
|---|---|---|
| 거래 5분봉 | 공개 linear GET /v5/market/kline, interval=5, start=O, end=C−1. UTC 시작 ms, OHLC, 수량, USDT 거래대금 | 세션당 한 페이지. [O,C) 안 모든 5분 시작 키와 일치 |
| mark 5분봉 | /v5/market/mark-price-kline, 같은 경계와 주기. UTC 시작 ms, mark OHLC | 거래봉과 같은 키, 봉 전체가 B 이전에 완결 |
| 정산율 | /v5/market/funding/history, startTime=O, endTime=C−1, limit=200. 심볼·정산 ms·부호 있는 율 | 8/7 16:00 UTC 전에는 API 기록, 그 시각부터 8시간 정시 기대 키와 일치 |
| 일별 거래대금 | 등록된 30일 창 합집합, 공개 거래 일봉 D, 연속 최대 30일 | #89의 일별 정규 행 SHA-256과 전수 일치. 용량 판정 재선택 없음 |
| 정산 mark | 정산 시각 t에 시작하는 mark 5분봉 시가 | 모든 기대 t가 세션 내 5분 경계이며 해당 봉이 있어야 동결 가능 |

모든 요청은 고정 호스트 api.bybit.com의 HTTPS GET만 쓴다. 각 명시 범위가 한 페이지에 들어가며 최신값 폴백·리디렉션·무범위 조회는 없다. 5분봉·펀딩은 전송 실패에만 같은 요청 최대 3회, 일봉은 #87의 정책을 따른다. 봉은 동일 키·동일값 중복을 한 번만 센다. 펀딩은 같은 값도 중복이면 무효이며 API의 단조 오름/내림차순만 허용하고 UTC 오름차순으로 보존한다. 충돌·범위 이탈·부분 페이지·잘못된 심볼은 원문을 보존하지 않고 중단한다. 중단 시 재실행 전 사고 시각·요청 범위와 이미 완료한 파일을 검토한다. [거래봉](https://bybit-exchange.github.io/docs/v5/market/kline), [mark 봉](https://bybit-exchange.github.io/docs/v5/market/mark-kline), [펀딩 이력](https://bybit-exchange.github.io/docs/v5/market/history-fund-rate)

## 재생 입력과 동결

캠페인 폴더의 daily/funding/mark/trade.jsonl은 각각 심볼·UTC ms 순이다. 각 행은 JSON 배열 [심볼, ms, 위 표의 나머지 필드 순서대로 정규 Decimal 문자열…], UTF-8 LF다. 파일 이름이 종류와 주기를 정한다. 예상·실제·결측 수를 검사하고 파일별 해시와 파일 해시 목록의 정규 JSON SHA-256을 manifest에 고정한다. 정정 이력은 최초 빈 배열이다. 실패한 부분 수집을 정상 manifest로 게시하지 않는다.

[증빙 JSON](D104-stage1-evidence/evidence.json)은 공지 본문(schedule), D103 사건 자료(halts_source), 추가로 확인한 사건(additional_events)을 해시로 연결한다. 두 원문과 적용 범위는 [증빙 부록](D104-evidence-review.md)에 있다. 주문 규격 네 값은 기존 11월 스냅샷 기준이다.

- 전환 시각 전에는 세션 안 5분 경계인 실제 응답 시각만 사용한다. 빈 목록도 가능하고 API 자체의 누락은 별도로 검출할 수 없다. 2026-08-07 16:00 UTC부터 그 시각을 포함해 8시간 간격으로 기대 키를 열거한다. 전환 세션의 전후를 독립 검사한다.
- 종목별 공지 사건 [start,end)이 세션과 겹치면 가격 요청 전 입력 검토로 중단한다. 나머지는 trade/mark 5분봉 전체 키 검사를 적용한다. 이 검사는 알려지지 않은 중단의 부재를 입증하지 않는다.
- 펀딩 금액에 쓰는 mark는 정산 시각 t에 시작하는 mark 5분봉 시가다. 실제 거래소 정산 mark와 차이가 있을 수 있는 사전 등록 대용값이며 결과를 보고 바꾸지 않는다. 보유 여부와 무관하게 t가 [O,C)의 5분 경계가 아니거나 해당 봉이 없으면 freeze에서 INVALID_INPUT으로 중단한다. 같은 시각 신규 진입분에는 소급 청구하지 않는다.

manifest에 등록 JSON 해시, 증빙·정규 행 파일 각각의 해시, 종목·종류별 수와 data_hash를 게시하고 검토한다. 출력은 manifest.json 한 파일이고 아래 run에는 **게시된 파일 해시**를 넘긴다. 재생은 파일 해시와 기대 키 검사를 다시 수행하며 네트워크 호출 없이 고정된 입력만 사용한다. 계산을 시작하면 run-started를 배타 생성한다. 실패 후 표식 삭제, 다른 폴더 재생, 종목이나 조건 교체를 하지 않는다.

## 첫 수집 중단과 재수집 결정

첫 캠페인은 SKHYNIXUSDT 거래봉의 2026-06-12 00:00~06:30 UTC 요청에서 중단됐다. 당시 실패 유형과 실제 누락 시각은 미상이다. [PR #92의 조사와 사용자 결정](https://github.com/JJang-Ah/RichLetsGo/pull/92#issuecomment-7163001246)에 따라 단건 조회를 생략하고 새 폴더에서 전체를 한 번 재수집한다. 이전 d104-stage1-data와 시작 표식은 보존하며 그 파일을 재사용하거나 결측을 보간하지 않는다.

요청 실패 시 새 캠페인의 `collect-failure.json`에 종류·종목·요청 시작/배타 끝(ms)·기대 행 수와 시도별 원인을 기록한다. transport는 전송 예외, exchange는 HTTP 오류 상태 또는 Bybit의 정수 오류 코드, format은 응답 형식·내용 검증 오류, missing_bars는 정상 해석한 페이지의 기대 키 누락이다. HTTP 오류는 이전과 같이 최대 3회 시도하고 Bybit 오류 응답은 즉시 중단한다. received_rows는 중복을 포함한 응답 목록 길이, received_bars는 완전성 검사를 수행한 페이지의 고유 키 수다. 확인할 수 없는 값은 null이다. 펀딩 기대 키 대조는 기존 동결 절차에서 수행한다.

누락이면 기록에 있는 missing_utc 전체를 게시하고 중단한다. 전송 실패만 반복되면 시도별 기록을 보고하고 수집 명령을 다시 실행하지 않는다. 거래소·형식 오류도 기록을 보고하고 중단한다. 실패 캠페인은 동결하거나 재생하지 않는다.

## 병합 후 PowerShell 절차

등록이 병합된 main checkout의 저장소 루트에서 시작한다. 코드 checkout과 캠페인 폴더는 서로 다른 새 경로다. 각 구간에서 명령 하나라도 실패하면 중단한다.

```powershell
$reg = (Resolve-Path 'docs/reports/D104-stage1-registration.json').Path
$bundle = (Resolve-Path 'docs/reports/D104-stage1-evidence').Path
$evidenceHash = (Get-Content -Raw -Encoding utf8 'docs/reports/D104-stage1-evidence-output.json' | ConvertFrom-Json).evidence_sha256
if ($evidenceHash -cne '24c9559ad4e07f52c068e8f3e24fc2cf4e1498471810b6104544bfe325d6e287') { throw 'evidence pin mismatch' }
$code = (Get-Content -Raw -Encoding utf8 $reg | ConvertFrom-Json).code
$parent = Split-Path -Parent (Get-Location).Path
$checkout = Join-Path $parent 'd104-stage1-recollection-code'
$campaign = Join-Path $parent 'd104-stage1-recollection-data'
git worktree add --detach $checkout $code
if ($LASTEXITCODE) { throw 'worktree failed' }
Set-Location $checkout
py -3.14 -m venv .venv
if ($LASTEXITCODE) { throw 'venv failed' }
$python = (Resolve-Path '.venv/Scripts/python.exe').Path
& $python -m pip install -e .
if ($LASTEXITCODE) { throw 'install failed' }
```

부록 병합 뒤 새 캠페인 폴더에 검토된 증빙만 복사하고 다시 검증한다.

```powershell
if (Test-Path -LiteralPath $campaign) { throw 'campaign already exists' }
Copy-Item -LiteralPath $bundle -Destination $campaign -Recurse -ErrorAction Stop
& $python tools/d104_stage1.py evidence --registration $reg --folder $campaign
if ($LASTEXITCODE) { throw 'evidence validation failed' }
```

증빙 JSON, 출처와 검증 결과(증빙 및 모든 참조 파일 SHA-256)를 가격 관측 전에 부록 PR로 게시하고 검토한다. 아래 명령에는 부록에 게시한 evidence_sha256을 사용한다. collect가 각 원자료까지 재검증한 후 첫 요청 전에 같은 해시를 collect-started에 기록한다.

```powershell
& $python tools/d104_stage1.py collect --registration $reg --folder $campaign --evidence-sha256 $evidenceHash
if ($LASTEXITCODE) { throw 'collection failed' }
```

수집 종료 후 같은 증빙 핀으로 동결한다. 게시값·수집 시작 표식·현재 증빙 원문이 다르면 실패한다.

```powershell
& $python tools/d104_stage1.py freeze --registration $reg --folder $campaign --evidence-sha256 $evidenceHash
if ($LASTEXITCODE) { throw 'freeze failed' }
```

freeze가 출력한 SHA-256과 manifest를 먼저 게시한다. 다음 단계에서 확정된 해시를 입력한다.

```powershell
$manifestHash = Read-Host '게시된 manifest.json SHA-256'
& $python tools/d104_stage1.py run --registration $reg --folder $campaign --evidence-sha256 $evidenceHash --manifest-sha256 $manifestHash
if ($LASTEXITCODE) { throw 'replay failed' }
```

## 결과 보고와 1회 판정

결과에는 11월 주문 규격 소급, 전환 전 API 정산 시각 사용, 중단의 공지 조사·봉 완전성 검사 대체 가정와 exploratory_only=true를 표시한다. 당시 주문 허용 여부, 수량·비용 및 거래 수는 모형과 다를 수 있다. GO도 기존 D104 검증 통과나 실거래 자격으로 사용하지 않는다.

- 집계 기간은 최초 상장 종목이 완결 UTC 일 30개를 채운 날짜부터 B 전날까지다. 무거래일의 순 R은 0이다. 공유 계좌 하나의 종료 거래 수, 최종 순자산·순손익·최저 순자산, 스트레스 포함 MDD와 최초 발생 봉 UTC ms, 재생 digest를 게시한다.
- 모든 GO 조건을 반올림 전 판정한다. N≥100, 거래당 평균 순 R>0, 날짜별 t≥2, 상위 ceil(N×1%) 제거 후 평균 순 R>0, 월별 합이 양수인 달 과반, 양수 심볼 기여 중 최대 비중≤50%다. 월별·심볼별 순손익과 순 R, 현금일, N과 N_min을 함께 표시한다. 유효 입력에서 원금 소진 또는 한 조건이라도 미달이면 NO_GO다. 원금 소진은 불리한 mark 스트레스 평가를 포함한 최저 순자산 ≤0이다. 자료 무효와 계산 중 예외는 INVALID_INPUT으로 기록하고 실행 표식을 유지한다.
- 전체와 심볼별로 슬리피지·양쪽 수수료·부호 있는 펀딩을 분리한다. 실현 슬리피지+수수료를 비용 전 가격 위험으로 나눈 비율의 nearest-rank p50/p90/p95와 ≥1 비율을 보고한다. 표본 0이면 비율은 null이다.
- 비용 필터 제외 수는 첫 신호의 예상 비용이 30%를 **초과한 경우**에만 한 번 센다. 전체·심볼·신호 시점 등급과 교차 집계를 보고한다. 이어서 예약 경쟁에서 기존 보유/3슬롯/총 명목 여유 부족으로 탈락하거나, 개장가 기준 개별 예약은 유효하지만 공유 명목 여유를 넘긴 경우를 심볼별 경합 탈락으로 센다. 비용 거절과 중복하지 않는다.
- 결과가 GO여도 2단계는 별도 등록까지 봉인한다. B는 현재 고정 상수이며 이번 PR은 2단계 실행 권한을 부여하지 않는다.

탐색 GO면 2단계 1회 개봉을 사용자가 승인할 수 있다. NO_GO·INDETERMINATE·INVALID_INPUT이면 2단계를 열지 않는다.
