# D98 07c 사전 등록 연구 실행 결과

## 실행 기록

- 실행일: 2026-11-09 (Asia/Seoul), 등록된 실행 1회, 재시작 0회.
- 등록 digest: `f7388c0737eafecee0ead281c8aea486c3155ba9c32303742a53273b3106d2e3`; 코드 C: `2026-11-09T05:41:31.823550+00:00`의 등록 정보에 고정된 `c235b28cebf4c29a0adaa465354e9ba01bbd0651`.
- 결과 본문 digest: `2cf057539b89ab85e36559dd9b139ab23cc4c615b9247e43e1c65706b4fbe307`; data_hash: `42acf4dfb41f5d9eff49c02c73a37cc5bc8d6c3d48de04b42382d0b04a08bc6c`; 감사 status: READY.
- 연구 판정: **NO_GO**.
- 결과 JSON SHA-256: `a91dd394e26308c1accb7e2e24bf4595b46ca30e5f7aa355f6375a104f30b3e4` (54829 bytes).
- 요약 MD SHA-256: `16cefefeec960a6adf15c0346df93d4a32e61e415b403f6062d58752294b2653` (1454 bytes).
- 거래 JSONL SHA-256: `4c3e98252ed27360fc6efa09e97fefea6e873ffa331de5d2a39c4540f3d71118` (6216836 bytes, 12186 trades). 거래 파일은 10MB 미만이어서 PR에 포함했다.
- 등록 실행 도구가 먼저 pool/audit/registration·C worktree·clean status·origin/main 게시본을 확인하고 audit를 재계산했다. data_hash가 등록값과 일치한 뒤 DB를 등록 digest로 freeze하고 계산했다. `state.json`: COMPLETED, restart_count 0.

## 단계별 개수

| 단계 | 건수 |
|---|---:|
| 평가 기회 | 1,819,280 |
| 메타데이터 부적격 | 130,022 |
| 메타데이터 자격 | 1,689,258 |
| 과거 입력 부족 | 0 |
| 입력 완결 후 데이터 부적격 | 1,059,509 |
| 입력 완결 후 데이터 자격 | 629,749 |
| 발생 거래 결과(유효 + 무효) | 12,186 |

결과 파일에는 데이터 부적격·무효 사유, 연도별/기술용 분포, 고정 진행 기준이 모두 포함돼 있다.

## 조합 결과

| 조합 | 유효 거래 | 평균 순 R | 상위 1% 제거 평균 R | 판정 불가 | 기준 1~3 |
|---|---:|---:|---:|---|---|
| v2-N42-k2 | 2,304 | -0.09170021541569578973710036230861007 | -0.1759161356202160764735410747902654 | False | [False, False, False] |
| v2-N42-k3 | 2,304 | 0.01477673833595431288718657546652371 | -0.07638483436305847684677231855826294 | False | [False, False, False] |
| v2-N84-k2 | 1,717 | -0.1126860504306579330171028097840194 | -0.2037740448787458271464683013755342 | False | [False, False, False] |
| v2-N84-k3 | 1,717 | 0.01181338802693922184474923904764179 | -0.07784300544694343265637580698263237 | False | [False, False, False] |
| v3-N42-k2 | 1,168 | -0.08622453993517771739425931496436062 | -0.1604946732720594044977055189458004 | False | [False, False, False] |
| v3-N42-k3 | 1,168 | 0.01840207992873134807698813457219829 | -0.06459969636718913364236981620954271 | False | [False, False, False] |
| v3-N84-k2 | 904 | -0.08823035210697363436279700917066662 | -0.1663674374090064207339367581505111 | False | [False, False, False] |
| v3-N84-k3 | 904 | 0.03061833461964027245577699261349686 | -0.05761558072770693683546342453702067 | False | [False, False, False] |

`NO_GO`는 사전 등록된 1단계 진행 기준 결과다. 이 보고서만으로 조건·매개변수를 다시 조정하지 않으며 판정용 풀에는 접근하지 않았다.

## DB 경로 해석

실행 인자로 지정한 DB는 `C:\J_PROGRAM\RichLetsGo\data\backtest\market_data\d98-design.sqlite`다. 이는 설정 예시 `config.example.toml`의 `paths.backtest.market_data_dir = "data/backtest/market_data"`를 메인 checkout `C:\J_PROGRAM\RichLetsGo` 기준으로 해석하고 파일명 `d98-design.sqlite`를 붙인 경로다. 별도 `config.local.toml`은 사용하지 않았다. 연구 CLI의 `--db` 인자로 이 절대 경로를 직접 넘겼다.

실행 결과 원본은 같은 디렉터리의 `D98-study.json`, `D98-study.md`, `D98-study-trades.jsonl`이다. JSON 안 execution 블록에는 실행 프로세스 감사 정보가 포함되고, 결과 본문 digest 계산에서는 제외된다.
