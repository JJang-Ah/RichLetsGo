# D102 펀딩 시장 중립 연구 1회 실행 절차

이 절차는 사전 등록 `b252a7781a9b3e52947fc1495c4e710aebe528765f63c0882ac40c4862183fcc`이 main에 병합된 뒤 **한 번만** 실행한다. 실행 담당은 Codex다(D100). 명령은 PowerShell 기준이고, 경로는 변수와 따옴표로 쓴다(D101 절차 실행 뒤 주석).

## 고정값

| 항목 | 값 |
|---|---|
| 코드 커밋 C | `1f2c014638069770706b0f1e2db9feb9568250d9` (PR #56 병합) |
| 등록 digest | `b252a7781a9b3e52947fc1495c4e710aebe528765f63c0882ac40c4862183fcc` |
| data_hash | `42acf4dfb41f5d9eff49c02c73a37cc5bc8d6c3d48de04b42382d0b04a08bc6c` |
| D98 freeze 등록 digest | `f7388c0737eafecee0ead281c8aea486c3155ba9c32303742a53273b3106d2e3` |
| DB | `C:\J_PROGRAM\RichLetsGo\data\backtest\market_data\d98-design.sqlite` (D98과 같은 파일, 읽기 전용) |

## 순서

1. 메인 checkout에서 등록이 병합된 main을 받는다.

   ```powershell
   $main = "C:\J_PROGRAM\RichLetsGo"
   git -C "$main" fetch origin
   git -C "$main" checkout main
   git -C "$main" pull --ff-only
   ```

2. C를 저장소 밖의 깨끗한 별도 worktree로 만든다. 경로는 예시이며 바꿔도 된다. 그 worktree에서도 `origin/main`에 등록이 보여야 한다.

   ```powershell
   $codeRoot = "C:\Users\tw5hy\Documents\Codex\d102-code-C"
   git -C "$main" worktree add "$codeRoot" 1f2c014638069770706b0f1e2db9feb9568250d9
   git -C "$codeRoot" fetch origin
   ```

3. C worktree **안에** 전용 venv를 만들고 그 worktree를 editable로 설치한다. 다른 checkout의 venv를 쓰면 CLI가 입력을 읽기 전에 거절한다. `.venv/`는 `.gitignore`에 있어 작업 트리가 깨끗하게 유지된다.

   ```powershell
   py -3.14 -m venv "$codeRoot\.venv"
   & "$codeRoot\.venv\Scripts\python.exe" -m pip install -e "$codeRoot"
   git -C "$codeRoot" status --porcelain   # 출력이 없어야 한다
   ```

4. 실행한다. 입력 세 파일은 **메인 checkout의 main**(등록 병합 이후)에서 읽는다.

   ```powershell
   $runArgs = @(
     "--preregistration", "$main\docs\reports\D102-study-preregistration.json",
     "--pool", "$main\docs\reports\D98-pool.json",
     "--audit", "$main\docs\reports\D98-collection-audit.json",
     "--db", "$main\data\backtest\market_data\d98-design.sqlite",
     "--output-root", "$main\backtest_results\d102-study",
     "--workers", "4"
   )
   & "$codeRoot\.venv\Scripts\python.exe" "$codeRoot\tools\d102_study.py" @runArgs
   ```

5. 도구는 계산 전에 아래를 확인한다. 하나라도 다르면 결과 파일을 만들지 않고 거절한다.
   - 등록 = `registration(C, pool, audit)`
   - worktree `HEAD = C`이고 깨끗함, C가 `origin/main`의 조상
   - 등록·풀·감사·D98 등록 파일이 `origin/main` 게시본과 바이트까지 같음
   - D98 등록 digest = freeze digest, DB(읽기 전용)의 freeze 기록 = D98 등록 digest, pool identity
   - 감사 재계산 data_hash = 게시값 = 등록값
6. 중단되면 같은 명령에 `--resume-crashed`를 붙여 다시 실행한다. 처음부터 다시 계산하며 결과 본문은 같아야 한다. `COMPLETED` 뒤 재호출은 연구를 다시 계산하지 않고 기존 결과만 돌려준다(스트림 sha256·Markdown 바이트·state digest 확인). 다만 5번 확인 단계(DB 감사 재계산·data_hash 포함)는 완료 상태 검사보다 앞이라 다시 돈다.
7. `$main\backtest_results\d102-study\<등록 digest>\`의 `D102-study.json`·`D102-study.md`·`D102-study-periods.jsonl`·`D102-study-positions.jsonl`을 `docs/reports/D102-study-result/`에 그대로 복사한다. 실행 기록 요약 `docs/reports/D102-study-result.md`를 써서 결과 PR로 게시한다. JSONL 파일이 10MB를 넘으면 그 파일은 빼고, 결과 JSON에 기록된 sha256·개수·크기를 요약에 적는다(합성 벤치마크에서 positions 스트림은 약 100MB였다).

## 하지 않는 것

- 결과를 본 뒤 조건·격자·코드·데이터를 바꾸는 것(바꾸면 새 가설이다). NO_GO여도 같은 가설을 다시 돌리지 않는다.
- 판정용 풀 데이터 접근. 결과가 GO여도 2단계 사전 등록 전까지 금지다.
- 실행 전 실제 DB로 시험 실행. 시험은 합성 데이터로만 한다.
- DB에 쓰기(freeze 다시 하기 포함). D102 도구는 읽기 전용으로만 연다.
