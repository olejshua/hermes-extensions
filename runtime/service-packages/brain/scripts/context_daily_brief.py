#!/usr/bin/env python3
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


HOME = Path.home()
INSTALLED_PROJECT_DIR = HOME / ".hermes" / "second-brain" / "app"
DEFAULT_PROJECT_DIR = INSTALLED_PROJECT_DIR if INSTALLED_PROJECT_DIR.is_dir() else Path(__file__).resolve().parents[1]
DEFAULT_CONTEXT_DB = HOME / ".hermes" / "second-brain" / "context-inbox.sqlite3"


def main() -> int:
    if any(arg in {"-h", "--help"} for arg in sys.argv[1:]):
        print(
            "usage: context_daily_brief.py [--help]\n\n"
            "Environment: PROJECT_DIR CONTEXT_DB CONTEXT_DAILY_BRIEF_HOURS "
            "HERMES_BRIEFING_PREFERENCES PYTHON HERMES_SECOND_BRAIN_MODULE"
        )
        return 0
    project_dir = Path(os.environ.get("PROJECT_DIR", DEFAULT_PROJECT_DIR))
    db = Path(os.environ.get("CONTEXT_DB", DEFAULT_CONTEXT_DB)).expanduser()
    module = os.environ.get("HERMES_SECOND_BRAIN_MODULE", "hermes_second_brain")
    python = os.environ.get("PYTHON", sys.executable)
    env = os.environ.copy()
    existing_pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(project_dir / "src") + (os.pathsep + existing_pythonpath if existing_pythonpath else "")
    hours = os.environ.get("CONTEXT_DAILY_BRIEF_HOURS", "24")
    result = subprocess.run(
        [python, "-m", module, "context-daily-brief", "--db", str(db), "--hours", hours],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        sys.stderr.write(result.stderr)
        return result.returncode
    sys.stdout.write(result.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
