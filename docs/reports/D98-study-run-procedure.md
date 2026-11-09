# D98 신호 연구 1회 실행 절차

이 절차는 사전 등록 `f7388c0737eafecee0ead281c8aea486c3155ba9c32303742a53273b3106d2e3`(이하 등록 P 문서)이 main에 병합된 뒤 **한 번만** 실행한다. 실행 담당은 Codex다(D100: 한 번만 하는 연구 실행은 Codex).

## 고정값

| 항목 | 값 |
|---|---|
| 코드 커밋 C | `c235b28cebf4c29a0adaa465354e9ba01bbd0651` (PR #47 병합) |
| 풀 digest | `8e5e2d0c4dfa32e0269dd9c906203a97026f552fab739bbe90c68d5368e206fe` |
| 수집 감사 digest | `dace1ad6783af04e31209a9bcd95911e9a585852d5518af175c44ee9bd667685` |
| data_hash | `42acf4dfb41f5d9eff49c02c73a37cc5bc8d6c3d48de04b42382d0b04a08bc6c` |
| DB | 설정의 `paths.backtest.market_data_dir` 아래 `d98-design.sqlite` (PR #44 수집본) |

## 순서

1. 메인 checkout에서 등록이 병합된 main을 받는다: `git fetch origin && git checkout main && git pull --ff-only`.
2. C를 깨끗한 별도 worktree로 만든다: `git worktree add <C 경로> c235b28cebf4c29a0adaa465354e9ba01bbd0651`. 그 worktree에서도 `git fetch origin`으로 `origin/main`에 등록이 보이게 한다.
3. C worktree **안에** 전용 venv를 만들고 그 worktree를 editable로 설치한다. 다른 checkout의 venv를 쓰면 CLI가 입력을 읽기 전에 거절한다(PR #47).
   - `py -3.14 -m venv <C 경로>/.venv` (`.venv/`는 `.gitignore`에 있어 `git status --porcelain`이 깨끗하게 유지된다. 다른 이름을 쓰면 미추적 파일이 되어 실행이 거절된다.)
   - `<C 경로>/.venv/Scripts/python -m pip install -e <C 경로>` (이하 `<venv>` = `<C 경로>/.venv`)
   - editable 설치가 `src/*.egg-info` 같은 파일을 만들면 `git status --porcelain`으로 깨끗한지 실행 전에 확인한다.
4. 실행한다. 입력 세 파일은 **메인 checkout의 main**(등록 병합 이후)에서 읽은 경로를 넘긴다.

   ```
   <venv>/Scripts/python <C 경로>/tools/d98_study.py ^
     --preregistration <메인>/docs/reports/D98-study-preregistration.json ^
     --pool <메인>/docs/reports/D98-pool.json ^
     --audit <메인>/docs/reports/D98-collection-audit.json ^
     --db <market_data_dir>/d98-design.sqlite ^
     --output-root <메인>/backtest_results/d98-study ^
     --workers 4
   ```

5. 도구가 계산 전에 확인하는 것: C worktree `HEAD = C`, 작업 트리 깨끗함, C가 `origin/main`의 조상, 세 입력이 `origin/main` 게시본과 같음, 감사 재계산의 `data_hash`·단계 개수 일치. 통과하면 DB를 등록 digest로 `Store.freeze`한 뒤 계산한다.
6. 중단되면 같은 명령에 `--resume-crashed`를 붙여 다시 실행한다(처음부터 다시 계산, 결과 본문은 같아야 함). `COMPLETED` 뒤 재실행은 계산 없이 기존 결과만 돌려준다.
7. `backtest_results/d98-study/<등록 digest>/`의 `D98-study.json`·`D98-study.md`를 `docs/reports/`에 그대로 복사해 결과 PR로 게시한다. `D98-study-trades.jsonl`은 10MB 이하면 함께 커밋하고, 넘으면 결과 JSON의 sha256만 적는다.

## 하지 않는 것

- 결과를 본 뒤 조건·격자·데이터를 바꾸는 것(바꾸면 새 가설이다).
- 판정용 풀 데이터 접근. 결과가 GO여도 2단계 사전 등록 전까지 금지다.
- 실행 전 실제 DB로 시험 실행. 시험은 합성 데이터로만 한다.

## 실행 뒤 주석 (2026-11-09, PR #49·#50 검토 P3)

- 4번 명령의 `^` 줄 연결은 cmd 문법이다. PowerShell에서는 백틱(`` ` ``) 줄 연결이나 인자 배열을 쓴다. 실제 실행(PR #50)은 같은 인자를 PowerShell에 맞게 넘겼다.
- 7번의 게시 위치는 실제로 `docs/reports/D98-study-result/`(원본 세 파일)와 요약 `docs/reports/D98-study-result.md`였다. 파일 내용은 실행 산출물과 바이트가 같다.
- DB 경로는 `config.example.toml`의 `market_data_dir`를 메인 checkout 기준으로 해석한 `C:\J_PROGRAM\RichLetsGo\data\backtest\market_data\d98-design.sqlite`였다(`config.local.toml` 없음).
