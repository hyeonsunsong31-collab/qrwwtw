import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import main  # noqa: E402


def test_runs_and_prints(capsys):
    assert main.main(["hello"]) == 0
    assert "hello" in capsys.readouterr().out
