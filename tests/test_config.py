from app.config import DEFAULT_DB_FILES, Settings


def test_default_db_file_differs_per_mode(monkeypatch):
    monkeypatch.delenv("GROUNDLINE_DB_PATH", raising=False)
    expected = {
        "mock": "./data/groundline.db",
        "dry_run": "./data/groundline-dryrun.db",
        "live": "./data/groundline-live.db",
    }
    paths = {mode: Settings(_env_file=None, mode=mode).db_path for mode in expected}
    assert paths == expected
    assert len(set(paths.values())) == 3


def test_explicit_db_path_argument_wins(monkeypatch):
    monkeypatch.delenv("GROUNDLINE_DB_PATH", raising=False)
    s = Settings(_env_file=None, mode="live", db_path="./custom.db")
    assert s.db_path == "./custom.db"


def test_db_path_from_environment_wins(monkeypatch):
    monkeypatch.delenv("GROUNDLINE_DB_PATH", raising=False)
    monkeypatch.setenv("GROUNDLINE_DB_PATH", "./x.db")
    s = Settings(_env_file=None, mode="live")
    assert s.db_path == "./x.db"


def test_empty_db_path_in_environment_uses_mode_default(monkeypatch):
    monkeypatch.delenv("GROUNDLINE_DB_PATH", raising=False)
    monkeypatch.setenv("GROUNDLINE_DB_PATH", "")
    s = Settings(_env_file=None, mode="live")
    assert s.db_path == DEFAULT_DB_FILES["live"]
