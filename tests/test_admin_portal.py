from pathlib import Path

from tgdeepseek.admin_portal import Dashboard, patch_yaml_scalars, read_yaml_scalars


def test_admin_yaml_patch_is_section_aware(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        "trigger:\n  max_replies_per_hour: 0\nproactive:\n  max_replies_per_hour: 8\n",
        encoding="utf-8",
    )
    patch_yaml_scalars(path, {("proactive", "max_replies_per_hour"): 40})
    values = read_yaml_scalars(path)
    assert values[("trigger", "max_replies_per_hour")] == 0
    assert values[("proactive", "max_replies_per_hour")] == 40


def test_admin_credentials_are_not_default_or_short() -> None:
    Dashboard("sq_admin_1234", "a-secure-password-with-32-characters")
