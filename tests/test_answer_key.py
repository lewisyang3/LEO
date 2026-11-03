import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_answer_key_validates():
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "validate_answer_key.py")],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
