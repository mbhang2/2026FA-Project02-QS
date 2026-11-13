"""docs/FILES.md is generated from each file's own description and kept current."""
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_the_file_map_is_current():
    r = subprocess.run([sys.executable, "scripts/gen_file_map.py", "--check"],
                       cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
