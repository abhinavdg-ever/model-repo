"""utilities/env_sync.py (2026-11-09): a .env rebuilt in its example's order, values kept."""
import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("env_sync", REPO / "utilities" / "env_sync.py")
env_sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(env_sync)


def test_values_kept_order_from_the_example(tmp_path, capsys):
    (tmp_path / ".env.example").write_text("# --- 1. A ---\nA=\nB=2\n\n# --- 2. C ---\nC=x\n", encoding="utf-8")
    (tmp_path / ".env").write_text("C=secret-c\nA=secret-a\nOLD=1\n", encoding="utf-8")
    assert env_sync.sync(tmp_path)
    assert (tmp_path / ".env").read_text(encoding="utf-8") == "# --- 1. A ---\nA=secret-a\nB=2\n\n# --- 2. C ---\nC=secret-c\n"
    out = capsys.readouterr().out
    assert "added B" in out and "dropped OLD" in out and "secret" not in out
    assert list(tmp_path.glob(".env.bak-*"))


def test_review_ui_and_its_client_copy_have_the_same_settings_file():
    """2026-11-09 (user): the same keys, order and comments; only the values differ."""
    import re

    a = (REPO / "review-ui" / ".env.example").read_text(encoding="utf-8").splitlines()
    client = REPO / "review-ui-client" / ".env.example"
    if not client.is_file():  # the client repo is a separate checkout
        return
    b = client.read_text(encoding="utf-8").splitlines()
    shape = lambda lines: [re.sub(r"=.*$", "=", l) for l in lines]
    assert shape(a) == shape(b)
