from pathlib import Path

from tgdeepseek.cli import project_root


def test_project_root_prefers_explicit_app_root(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("APP_ROOT", str(tmp_path))
    assert project_root() == tmp_path.resolve()


def test_project_root_uses_working_project(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("APP_ROOT", raising=False)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "config.yaml").write_text("{}", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert project_root() == tmp_path.resolve()
