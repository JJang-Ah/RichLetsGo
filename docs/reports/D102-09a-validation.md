# D102 09a 합성 검증

09a는 연구 도구 구현 단계다. 실제 D98 설계 DB를 열거나 연구를 실행하지 않았고, 판정용 풀에도 접근하지 않았다. 아래 SQLite 입력은 모두 테스트 임시 폴더에 생성한 합성 데이터다.

## 구현 경계

- `d102/eligibility.py`, `funding.py`: 계약별 시간봉만 읽어 매일 자격을 분류한다. 두 L의 점수가 모두 유효해야 하며, 후보 선택에는 t 이후 데이터를 쓰지 않는다.
- `d102/study.py`: 계약별 worker가 작은 특징·기간 결과 파일을 만들고, 부모가 (t, symbol) 순서로 병합해 같은 날의 후보만 들고 네 바구니를 계산한다. 전체 계약의 시간봉을 함께 메모리에 올리지 않는다.
- `d102/portfolio.py`, `basket.py`, `period.py`: 부호 있는 목표·등급을 유지하고, 무효 기여 0·폐지 뒤 현금·마지막 생존 보유 청산을 처리한다. 무효 기간은 결과 JSONL에는 남고 통계에서는 제외한다.
- `d102/statistics.py`: 기준 5는 정상 기간의 유효 종목 기여만 모으고, 제거 후에도 원래 유효 기간 수를 분모로 유지한다. 기술용 최악 종목·기간과 유동성별 기여도 이 기여 집합을 사용한다. 비용은 기간 단위로 따로 보고한다.
- `d102/execution.py`: D101의 읽기 전용 freeze/data_hash 검사 함수를 import한다. D102 정책·등록·출력 경로는 별도다. C checkout과 나중에 main에 게시한 등록 파일의 바이트를 검사한 뒤 계산한다.
- 완료 결과 재호출은 감사 검사를 다시 수행하지만 연구를 재계산하지 않는다. Markdown은 최초 생성과 재호출 모두 JSON과 같은 정규화 표현을 쓴다. 중단된 RUNNING은 `--resume-crashed` 없이는 거절한다.

## 명세 완료 조건과 테스트

| 조건 | 합성 테스트 |
|---|---|
| 1·2·4·8h 환산, 역사적 주기 변경, 창 경계·신선도 | `test_d102_primitives.py` |
| 744h 입력, 사유 우선순위, 두 L, D1..D30, 20M 포함 경계 | `test_d102_period.py` |
| n24/25, 단일 순위 양끝, 접두어 동률, 동일 자산 계약 하나 | `test_d102_primitives.py`, `test_d102_study.py` |
| 부호 반전·등급 보존·현금 청산·최종 생존 보유 비용 | `test_d102_primitives.py`, `test_d102_period.py` |
| 가격·펀딩 방향과 가격 비율, 폐지 Ea<e/Ea=e, 중복 비용 방지 | `test_d102_period.py` |
| 시장 전체 같은 가격 변동 시 가격 몫 0, 손계산 펀딩 | `test_d102_period.py` |
| 무효 종목 비중 유지, 전체 다리 무효 뒤 보유 유지·현금 전환 | `test_d102_period.py` |
| n100, t=2, s=0, n<2, ceil 절사, 20기간 해·엄격 과반 | `test_d102_study.py` |
| 초기 자산 최고점·25% 경계·파산, 기준5 분모·동률, 5% 경계 | `test_d102_study.py` |
| 판정용 poison 레코드 미조회, 1/2 worker 바이트 동일, LF | `test_d102_study.py` |
| C/main 등록 바이트, 완료 재호출, 변조, 명시적 재개, import root | `test_d102_execution.py` |
| 읽기 전용·freeze·data_hash 거절, D98 결과 digest 회귀 | 기존 `test_d101_study.py`의 재사용 함수 회귀 |

## 검증 실행

- D102 합성 테스트: 30 passed.
- D101·D98 연구 테스트: 58 passed.
- D98 수집·실행 경계·풀·통계 테스트: 115 passed.
- 저장소 전체 Ruff check/format, mypy: 통과.
- CLI `--help`: 통과.

전체 백테스트 스위트는 이번 검증에서 실행하지 않았다. 검증 범위는 변경된 연구 도구와 재사용한 D98·D101 경로다.

600계약 × 43,000봉 측정값과 worker digest 비교는 `D102-09a-synthetic-benchmark.json` 및 동명의 Markdown에 별도로 기록한다. 벤치마크 도구는 `TemporaryDirectory`에서 합성 DB를 생성하고 종료 시 제거한다.

다음 단계는 Claude의 구현 검토·병합 후 09b 사전 등록이다. 실제 DB 실행은 등록 병합 이후 09c에서만 허용한다.
