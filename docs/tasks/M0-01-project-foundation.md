# M0-01 프로젝트 기반

- 상태: 명세 작성 완료, 사용자 확인 대기
- 구현: Codex / 검토: Claude Code (D37)
- 브랜치: `task/M0-01-project-foundation`
- 근거: [05_DEVELOPMENT_PROCESS.md](../design/05_DEVELOPMENT_PROCESS.md) 3장 M0·5장, [03_SYSTEM_ARCHITECTURE.md](../design/03_SYSTEM_ARCHITECTURE.md) 1장·3장, REVIEW.md 발견 9(`.gitignore`) 및 "프로젝트 구성에서 부족한 점"

## 목표

이후 마일스톤이 같은 기준 위에서 시작하도록 패키지 구조, Python 버전, 도구, CI를 갖춘다. **매매 로직은 넣지 않는다.**

## 범위

### 만들 것

1. **패키지 구조** (src 레이아웃, 패키지 이름 `richletsgo`)

   ```
   pyproject.toml
   src/richletsgo/
     __init__.py          __version__ = "0.0.1"
     config/  keystore/  marketdata/  strategy/  portfolio/
     ai/  risk/  oms/  storage/  notify/  health/
     exchange/
       __init__.py
       bybit/  simulated/
   src/richletsgo_watchdog/__init__.py
   src/richletsgo_dashboard/__init__.py
   db/migrations/.gitkeep
   tests/
     test_smoke.py
   config.example.toml
   .gitattributes
   .github/workflows/ci.yml
   ```

   - 각 하위 패키지는 `__init__.py`에 03_SYSTEM_ARCHITECTURE.md 3장의 역할을 한 줄 docstring으로만 적는다. 다른 코드는 넣지 않는다.
   - `keystore`라는 이름을 쓴다. `secrets`는 표준 라이브러리와 겹친다.

2. **`pyproject.toml`**
   - `requires-python = ">=3.12,<3.13"` (D40)
   - 런타임 의존성: 없음 (M3에서 추가)
   - 개발 의존성 (`[project.optional-dependencies] dev`): `pytest`, `ruff`, `mypy`. 버전 하한을 적는다.
   - 빌드 백엔드: `setuptools` (추가 도구 설치 없이 `pip install -e .[dev]`로 설치 가능해야 함)
   - `ruff`: 기본 규칙 + `I`(import 정렬), `B`, `UP`. 줄 길이 100.
   - `mypy`: `src/richletsgo*`에 `strict = true`.
   - `pytest`: `testpaths = ["tests"]`.

3. **스모크 테스트** `tests/test_smoke.py`
   - `richletsgo`와 모든 하위 패키지가 import되는지 확인한다.
   - Python 버전이 3.12인지 확인한다.

4. **기존 코드 유지**
   - `trading_lab/`과 `tests/test_core.py`는 이번에 지우지 않는다(M1에서 이전 후 삭제, D39).
   - 기존 테스트 9개가 `pytest`로 그대로 통과해야 한다. `mypy`와 `ruff` 대상에서는 `trading_lab/`을 제외한다.
   - 주의: `tests/`에 `__init__.py`가 없으면 pytest는 저장소 루트를 import 경로에 넣지 않아 `import trading_lab`이 실패한다. 임시로 pytest 설정에 `pythonpath = ["."]`를 넣고, M1에서 `trading_lab`을 지울 때 함께 뺀다.

5. **CI** `.github/workflows/ci.yml`
   - 트리거: 모든 PR, `main` push
   - 실행 환경: `windows-latest` (운영 환경과 같은 OS), Python 3.12
   - 단계: 의존성 설치 → `ruff check .` → `ruff format --check .` → `mypy` → `pytest`
   - 비밀 정보를 쓰지 않는다. 네트워크가 필요한 테스트는 없다.

6. **`.gitignore` 정리** (REVIEW.md 발견 9)
   - 이름에 단어가 포함된 모든 파일을 지우는 패턴(`*secret*`, `*credentials*`)을 없앤다.
   - 대신 구체적인 파일만 제외한다: `.env`, `.env.*`, `config.local.toml`, `*.key`, `*.pem`
   - 루트 기준으로 좁힌다: `/data/`, `/logs/`, `/backups/`, `/backtest_results/`
   - 도구 산출물: `.venv/`, `__pycache__/`, `*.pyc`, `.pytest_cache/`, `.mypy_cache/`, `.ruff_cache/`, `*.egg-info/`, `build/`, `dist/`
   - 로컬 DB 파일: `*.sqlite`, `*.sqlite3`, `*.db`
   - `!.env.example` 규칙은 지운다(해당 파일을 만들지 않음).

7. **`.gitattributes`**
   - `* text=auto eol=lf`. 이렇게 하면 지금 커밋마다 나오는 LF/CRLF 경고가 없어진다.
   - Windows 전용 스크립트(`*.ps1`, `*.bat`, `*.cmd`)만 `eol=crlf`.

8. **`config.example.toml`** (비밀 정보 없음)
   - `environment = "backtest"` (`backtest` / `demo` / `live`)
   - `[bybit]` `category = "linear"`, `symbols = ["BTCUSDT", "ETHUSDT"]`
   - `[risk]`의 모든 수치는 주석 처리된 자리 표시와 근거 문서 링크만 둔다(Q13 미정).
   - `[keystore]` 자격 증명 관리자 항목 **이름**만 적는다(예: `bybit_demo_api = "richletsgo/bybit-demo"`). 값은 절대 넣지 않는다.
   - 실제 설정 파일 `config.local.toml`은 `.gitignore`로 제외한다.

9. **README 개발 환경 절**
   - Python 3.12 설치, `.venv` 만들기, `pip install -e .[dev]`, 검사 명령 4개(ruff check, ruff format --check, mypy, pytest)를 PowerShell 기준으로 적는다.

### 만들지 않을 것

- 매매·위험·주문·전략 로직, 데이터 모델 (M1)
- 네트워크 연결, 거래소 클라이언트, Oracle 연결 (M3)
- 런타임 의존성 추가
- `trading_lab/` 삭제 (M1)
- pre-commit 훅, 릴리스 자동화

## 완료 조건

- [ ] PR의 CI(Windows, Python 3.12)가 통과한다.
- [ ] 로컬 Python 3.12 `.venv`에서 `ruff check .`, `ruff format --check .`, `mypy`, `pytest`가 모두 통과한다.
- [ ] `pytest` 결과에 기존 테스트 9개와 스모크 테스트가 포함된다.
- [ ] `git check-ignore`로 확인했을 때 `src/richletsgo/keystore/__init__.py`는 제외되지 **않고**, `config.local.toml`, `.env`, `data/x.csv`는 제외된다. 이 확인 결과를 PR 설명에 붙인다.
- [ ] `config.example.toml`에 비밀 값이 없다.
- [ ] HANDOFF.md에 변경·검증·남은 작업을 기록한다.

## 검토 포인트 (Claude)

- 패키지 구조가 03_SYSTEM_ARCHITECTURE.md 3장과 일치하는지, 하위 패키지에 로직이 들어가지 않았는지
- `.gitignore`가 소스 파일을 실수로 제외하지 않는지
- CI가 `main`과 PR 모두에서 돌고, 실패 시 PR이 막히는지(브랜치 보호 설정은 사용자가 GitHub에서 켠다)
- 개발 의존성 버전 하한과 Python 3.12 고정

## 사용자 작업

- 이 PC에 Python 3.12 설치 (현재 `py -3`은 3.14.3)
- PR이 병합된 뒤 GitHub 저장소 설정에서 `main` 브랜치 보호: PR 필수, CI 통과 필수
