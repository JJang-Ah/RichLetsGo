# D101 신호 연구 1회 실행 절차

이 절차는 사전 등록 `e2d0cec79f829bcbabc10322c6a43ab0e975a39f04f2a27dc534e60e383fb314`이 main에 병합된 뒤 **한 번만** 실행한다. 실행 담당은 Codex다(D100). 명령은 PowerShell 기준이다.

## 고정값

| 항목 | 값 |
|---|---|
| 코드 커밋 C | `303ef3d742e099076bfb683fa1e7cdb69b3953d6` (PR #52 병합) |
| 등록 digest | `e2d0cec79f829bcbabc10322c6a43ab0e975a39f04f2a27dc534e60e383fb314` |
| data_hash | `42acf4dfb41f5d9eff49c02c73a37cc5bc8d6c3d48de04b42382d0b04a08bc6c` |
| D98 freeze 등록 digest | `f7388c0737eafecee0ead281c8aea486c3155ba9c32303742a53273b3106d2e3` |
| DB | `C:\J_PROGRAM\RichLetsGo\data\backtest\market_data\d98-design.sqlite` (D98과 같은 파일, 읽기 전용) |

## 순서

1. 메인 checkout에서 등록이 병합된 main을 받는다.

   ```powershell
   git -C C:\J_PROGRAM\RichLetsGo fetch origin
   git -C C:\J_PROGRAM\RichLetsGo checkout main
   git -C C:\J_PROGRAM\RichLetsGo pull --ff-only
   ```

2. C를 깨끗한 별도 worktree로 만든다(`<C>`는 저장소 밖 경로). 그 worktree에서도 `origin/main`에 등록이 보여야 한다.

   ```powershell
   git -C C:\J_PROGRAM\RichLetsGo worktree add <C> 303ef3d742e099076bfb683fa1e7cdb69b3953d6
   git -C <C> fetch origin
   ```

3. C worktree **안에** 전용 venv를 만들고 그 worktree를 editable로 설치한다. 다른 checkout의 venv를 쓰면 CLI가 입력을 읽기 전에 거절한다. `.venv/`는 `.gitignore`에 있어 작업 트리가 깨끗하게 유지된다.

   ```powershell
   py -3.14 -m venv <C>\.venv
   & <C>\.venv\Scripts\python.exe -m pip install -e <C>
   git -C <C> status --porcelain   # 출력이 없어야 한다
   ```

4. 실행한다. 입력 세 파일은 **메인 checkout의 main**(등록 병합 이후)에서 읽는다.

   ```powershell
   $main = "C:\J_PROGRAM\RichLetsGo"
   $runArgs = @(
     "--preregistration", "$main\docs\reports\D101-study-preregistration.json",
     "--pool", "$main\docs\reports\D98-pool.json",
     "--audit", "$main\docs\reports\D98-collection-audit.json",
     "--db", "$main\data\backtest\market_data\d98-design.sqlite",
     "--output-root", "$main\backtest_results\d101-study",
     "--workers", "4"
   )
   & <C>\.venv\Scripts\python.exe <C>\tools\d101_study.py @runArgs
   ```

5. 도구가 계산 전에 확인하는 것: 등록 = `registration(C, pool, audit)`, worktree `HEAD = C`, 깨끗함, C가 `origin/main`의 조상, 등록·풀·감사·D98 등록 파일이 `origin/main` 게시본과 바이트까지 같음, DB(읽기 전용)의 freeze 기록 = D98 등록 digest, pool identity, 감사 재계산 data_hash = 게시값 = 등록값. 하나라도 다르면 결과 파일을 만들지 않고 거절한다.
6. 중단되면 같은 명령에 `--resume-crashed`를 붙여 다시 실행한다(처음부터 다시 계산, 결과 본문은 같아야 함). `COMPLETED` 뒤 재실행은 계산 없이 기존 결과만 돌려준다.
7. `backtest_results\d101-study\<등록 digest>\`의 `D101-study.json`·`D101-study.md`·`D101-study-trades.jsonl`을 `docs/reports/D101-study-result/`에 그대로 복사하고, 실행 기록 요약 `docs/reports/D101-study-result.md`를 써서 결과 PR로 게시한다. 거래 파일이 10MB를 넘으면 파일은 빼고 결과 JSON의 sha256·크기만 적는다.

## 하지 않는 것

- 결과를 본 뒤 조건·격자·코드·데이터를 바꾸는 것(바꾸면 새 가설이다).
- 판정용 풀 데이터 접근. 결과가 GO여도 2단계 사전 등록 전까지 금지다.
- 실행 전 실제 DB로 시험 실행. 시험은 합성 데이터로만 한다.
- DB에 쓰기(freeze 다시 하기 포함). D101 도구는 읽기 전용으로만 연다.

## 실행 뒤 주석 (2026-11-10, PR #53·#54 검토 P3)

- 2~4번의 `<C>`는 자리 표시라 그대로 붙여 넣으면 PowerShell 파서 오류가 난다. 실제 실행(PR #54)은 worktree 경로로 바꾸고 따옴표를 쳤다. D102 절차부터는 `$codeRoot` 변수를 쓴다.
- 6번: `COMPLETED` 뒤 재호출은 연구를 다시 계산하지 않지만, 확인 단계(DB 감사 재계산·data_hash)는 완료 상태 검사보다 앞이라 다시 돈다.
- 결과 Markdown의 수치 표현(끝자리 0 유지)과 JSON 정규화 표현이 달라서, 완료 결과 재호출 때 `_completed`가 MD 바이트 비교에서 거절할 수 있다. 결과 값에는 영향이 없다. D101은 다시 실행하지 않는다.
