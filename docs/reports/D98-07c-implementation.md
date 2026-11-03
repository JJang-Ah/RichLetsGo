# M2-07c 구현·합성 검증

이 문서는 구현 검증이다. 실제 설계 풀 연구 결과나 사전 등록이 아니다. 실제 d98-design.sqlite 및 판정용 풀은 열거나 실행하지 않았다. 기준은 04 3.3 3·4와 M2-07의 07c이며, 신호 방향·과열 조건은 01 1.2b를 함께 적용했다.

## 구현

- 계약별 4h 봉·고정 창 EMA/ATR·30일 거래대금·실제 직전 정산 3개·BTC 일봉 상태. 메타/입력/데이터 단계와 지표는 조합 사이에 공유하고 (v,N) 신호, k 거래 순서로 계산한다.
- 독립 수량 1 거래, 고정 R 분모, t 정산 제외/청산 정산 포함, 시간 청산·추적·손절 순서, 폐지 Ea의 2% 및 close(La) 정산. 모든 금액 계산 Decimal precision 34.
- 계약별 결과를 임시 JSONL에 쓴 뒤 (t, symbol, combination) 순서로 병합한다. 집계는 스트리밍, 상위 1%만 heap 보존한다. 동일 R 동률은 이른 t/심볼 오름차순으로 제거한다.
- 같은 조합 기준 1~3, 고정 분모 8의 기준 4, 정확히 5% 허용, 0신호/판정 불가 제외. 기술용 분포는 관측/NA 개수·평균·최소/최대·고정 구간 histogram을 제공한다.
- 등록 JSON/MD 작성 함수, C/P 분리 실행 CLI, 공개된 origin/main 사본·코드·조건·데이터 해시 검증, 계산 전 Store.freeze. 같은 등록 동결은 멱등이며 다른 등록은 거절한다.
- 실행 표식 O_EXCL + OS 잠금으로 동시 실행/재개를 직렬화한다. DB 잠금에도 OS gate를 추가했다. 강제 종료 시에는 자기 등록의 실행 이력과 일치하는 DB 표식만 명시적 재개로 복구한다. 기존 수집/감사의 정수 PID 표식은 자동으로 회수하지 않는다.
- 결과 JSON 확정 후 COMPLETED 이전 종료는 계산 없이 파일 digest를 검증하고 표식을 정리한다. 재시작 감사 정보는 결과 본문 digest에서 제외한다. JSON/MD/JSONL은 UTF-8 LF로 쓴다.

## 완료 조건 대응

| 조건 | 합성 검증 |
|---|---|
| 등록/코드/조건/data_hash 불일치, 감사 진행 불가 | test_d98_execution: 계산·동결 전에 거절, BTC_INCOMPLETE/STUDY_UNDECIDABLE 종료 |
| C 코드 → P 등록 → C worktree | 임시 git repo/worktree 성공, tracked/untracked 변경 및 P HEAD 거절 |
| 한 번 실행·재개·rename 복구 | live owner/OS gate 거절, COMPLETED 무계산 재사용, 재개 본문 digest 동일, 실제 os._exit 강제 종료 2경로 |
| 4h/V/돌파/EMA/ATR | test_d98_study: 누락·신호봉 제외·엄격 비교·EMA 기대값·기존 Wilder ATR 교차 확인 |
| 펀딩·BTC·단순 거래·폐지 | 1/2/4/8h, 경계, 초기 추세 유지, BTC 갱신, 갭/봉 내 손절, 비소급 추적, H=168h, 손절 사유 우선, Ea/초과 꼬리 |
| 두 비율·진행 기준·동률 | test_d98_statistics: 정확히 5%/초과, 0신호, 같은 조합, ceil, 심볼 prefix 동률, 연도 표본·과반 |
| 동시 활성 계약 하나 | 우선순위 낮은 계약 기회 0·거래 없음; 판정용 poison 행에 접근하지 않음 |
| 폐지 시각 정산 R 연결 | 합성 응답 → 실제 collect → audit/hash → study/trade, Ea rate만 바꾸면 hash와 R 모두 변경 |
| 과거 정산 주기 변경 | 240분→60분 전환의 실제 직전 정산 사용; 현재 60분 메타와 과거 240분이 다른 풀 fixture |
| Store.freeze | 계산 진입 전 동결 확인, 동일/다른 digest, 쓰기 거절 |
| LF 바이트 | 등록·결과 JSON/MD·거래 JSONL, git blob과 등록 파일 바이트 일치 |
| worker 결정성 | 소형 전체 결과/거래 바이트 동일 + 아래 600계약 벤치마크 |

Python 3.14: 전체 **927 passed (77.72s)**. 이후 수집 연결 fixture 보강은 해당 **17 passed (1.27s)**로 재검증했다. ruff check/format 및 mypy strict 98 source files 통과. 전체 asyncio 테스트는 Windows 샌드박스의 loopback accept 제한을 피하기 위해 동일 명령을 샌드박스 밖에서 실행했다.

## 대규모 합성 벤치마크

도구: `tools/benchmark_d98_study.py --output <새 결과 JSON 경로> --contracts 600 --bars 43000 --workers 4`.

- 600계약 × 43,000봉 = 25,800,000행, 6,230,400 평가 기회, 유효 거래 273,600건, 거래 파일 99,590,400 bytes.
- 매 720h마다 마지막 24h 거래대금 6배, 마지막 4h 가격 돌파. 실제 지표·신호·8조합 거래·집계 코드를 실행한다. 무신호 전용 부하가 아니다. 방향은 LONG인 합성 workload이며 SHORT 경로는 단위 테스트로 검증한다.
- 저장 218.088s / 최대 프로세스 메모리 39.95 MiB. 감사·해시 4 workers 76.087s.

| 연구 workers | 경과 | 부모 최대 MiB | worker별 최대 MiB | 최대치 합계 상한 MiB |
|---|---:|---:|---|---:|
| 1 | 1,563.900s (26m04s) | 115.64 | 부모 내 실행 | 115.64 |
| 4 | 401.847s (6m42s) | 115.64 | 95.47 / 95.45 / 95.67 / 95.40 | 497.63 |

메모리는 Windows PeakWorkingSetSize 기반이며, 합계는 동시 RSS 실측이 아니라 프로세스별 최대치의 보수적 합계다. 부모 최대치는 앞선 단계도 포함하는 프로세스 수명 최대치다. 데이터 특성/신호 빈도에 따라 실제 실행 시간은 달라진다.

- 1/4 worker 전체 결과 본문 digest: `593b7a0280f0cb10b4e99e4ff5b312f16827583b6bfae8c88a5afd3e667678bf`
- 1/4 worker 거래 바이트 SHA-256: `74341cd622f43145d9620b3ea0f810039f35a7923d068db33852da7f4e556060`
- 합성 data_hash: `65c5317bb6e0c50c5e25b91853baf36761fa8ffbe3c56ca23b6a9266cf550463`
- 원시 측정: [D98-study-synthetic-benchmark.json](D98-study-synthetic-benchmark.json). 파일 SHA-256: `ceb1ed6cbdb24cdb46b357948140ff3d83dfc750f9fa0f3b9e6cc3b751594ee3`.
- DB와 거래 파일은 TemporaryDirectory 안에서 만들고 실행 후 삭제했다(`temporary_database_removed=true`). 저장소에는 측정 JSON만 남긴다.

## 검토 요청·다음 순서

1. 봉 내 손절은 실제 초 단위 체결시각을 알 수 없으므로 `exit_time`에 해당 1h 봉 시작 라벨을 기록했다. 가격·펀딩 계산은 04의 순서를 따른다. 이 보고 필드 표현이 적절한지 검토 요청한다(거래 규칙 변경 아님).
2. 기술용 분포의 고정 histogram 구간은 통과 기준과 무관한 출력 표현이다. JSON의 `upper_inclusive_bins`와 NA 개수를 함께 검토해 달라.
3. 구현 검토·병합 후 코드 C로 사전 등록을 **별도 게시·병합**한다. 이 PR은 등록/실제 연구 실행을 하지 않는다. `execution.registration(C,pool,audit)` 및 `publish_registration(directory,value)`로 등록 문서를 만들 수 있다.
4. 등록 병합 뒤 C의 깨끗한 별도 worktree에서 그 worktree 코드가 import되는 환경을 준비한다. `tools/d98_study.py --preregistration <P의 등록 JSON> --pool <P의 풀 JSON> --audit <P의 감사 JSON> --db <DB 경로> --output-root <출력 부모> --workers 4`로 실행한다. 경로는 모두 인자로 받으며 루트/사용자 경로를 고정하지 않는다. origin/main은 등록을 포함하도록 fetch되어 있어야 한다.
5. 실제 연구 1회 실행·결과 게시와 판정용 접근은 이 구현 검증에 포함되지 않는다. 완료 조건의 마지막 수동 실행 항목은 의도적으로 미완료다.
