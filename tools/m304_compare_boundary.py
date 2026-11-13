"""Compare synthetic campaign bodies with a pre-extraction runner; never open user data.

Run with the repository venv: python tools/m304_compare_boundary.py --baseline b36b4d2
All market DBs and reports live in TemporaryDirectory and are removed on exit.
"""

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

CHILD = """import sys,json,time
from pathlib import Path
import richletsgo.backtest.runner as runner
if sys.argv[2] == "old":
    exec(compile(Path(sys.argv[3]).read_text(encoding="utf-8"), "baseline_runner.py", "exec"), runner.__dict__)
sys.path.insert(0, str(Path.cwd()/"tests"))
from test_walkforward import fixture_config,NOW
from richletsgo.backtest.walkforward import run_campaign
from richletsgo.backtest.codec import digest,encode
if __name__ == "__main__":
    folder=Path(sys.argv[1]); folder.mkdir()
    path,_=fixture_config(folder)
    start=time.perf_counter()
    report=run_campaign(path,"m304-synthetic-comparison",NOW)/"report-data.json"
    body=json.loads(report.read_text(encoding="utf-8"))
    content=(encode(body)+"\\n").encode("utf-8")
    (folder/"body.json").write_bytes(content)
    print(json.dumps({"variant":sys.argv[2],"seconds":round(time.perf_counter()-start,3),"bytes":len(content),"digest":digest(body)}))
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    old = subprocess.check_output(
        ["git", "show", args.baseline + ":src/richletsgo/backtest/runner.py"],
        cwd=root,
        text=True,
        encoding="utf-8",
    )
    with tempfile.TemporaryDirectory(prefix="m304-campaign-") as folder:
        base = Path(folder)
        old_path = base / "old_runner.txt"
        old_path.write_text(old, encoding="utf-8")
        script = base / "check.py"
        script.write_text(CHILD, encoding="utf-8")
        for variant in ("old", "new"):
            subprocess.run(
                [sys.executable, str(script), str(base / variant), variant, str(old_path)],
                cwd=root,
                check=True,
            )
        assert (base / "old/body.json").read_bytes() == (base / "new/body.json").read_bytes()
        print("Canonical campaign report body bytes: IDENTICAL")


if __name__ == "__main__":
    main()
