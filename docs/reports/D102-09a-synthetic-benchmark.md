# D102 09a 합성 벤치마크

실제 시장 DB를 사용하지 않았다. 600계약 × 43,000시간봉(25,800,000봉)을 임시 SQLite에 만들고, 같은 입력으로 1/4 worker 연구를 비교했다.

| 단계 | 시간(초) | 최대 프로세스 트리 working set(MiB) |
|---|---:|---:|
| 합성 DB 생성 | 147.993 | 36.48 |
| 연구 1 worker | 219.229 | 165.36 |
| 연구 4 worker | 84.52 | 462.87 |

- Body SHA-256 동일: `b7f54abe0a777135c07d9fd05a15db3b8a7308f3bc1b36d8877b4780e32db4dd`
- 종목·기간 stream SHA-256 동일: `be6a0d22d4ce811cfaeffd5f94650ac145319d775b14427b7dcfcfec99b3b276`
- 각 실행 종목·기간 395,520개, 101,060,160 bytes. 파일 바이트도 직접 비교했다.
- 모든 조합의 기간 stream 해시는 body에 포함되어 같은 body digest로 함께 검증된다.
- 임시 SQLite(약 2.56GB) 및 결과 디렉터리 삭제 확인.
- 메모리는 Windows 프로세스 트리 working set을 0.2초 간격으로 표본 측정한 최대값이다(순간 피크의 상한은 아님).
- 벤치마크 가격·펀딩은 고정값이다. 단위·통합 테스트에서는 서로 다른 펀딩, 가격 변동, 무효, 폐지, 비용과 재개를 따로 검증했다.

재현 명령(저장소 루트):

```powershell
.\.venv\Scripts\python.exe tools/benchmark_d102_study.py --contracts 600 --hourly-bars 43000 --workers 4
```
