import importlib.util
from pathlib import Path

_MODULE_PATH = Path(__file__).parents[2] / "scripts" / "production_env_check.py"
_SPEC = importlib.util.spec_from_file_location("production_env_check", _MODULE_PATH)
assert _SPEC and _SPEC.loader
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)

environment_file_values = _MODULE.environment_file_values
unexpected_templates = _MODULE.unexpected_templates
unexpected_installed = _MODULE.unexpected_installed
ROOT = Path(__file__).parents[2]


def test_environment_file_values_reads_every_binding_and_ignores_the_optional_marker() -> None:
    text = (
        "[Service]\n"
        "# EnvironmentFile=/home/quant/repos/ignored/.env.local\n"
        "EnvironmentFile=-%h/.config/market-data-center/env\n"
        "EnvironmentFile=/home/quant/.config/market-data-center/env /tmp/second.env\n"
        "Environment=DATACENTER_CANONICAL_ROOT=/home/quant/market_lake/canonical\n"
    )

    assert environment_file_values(text) == [
        "%h/.config/market-data-center/env",
        "/home/quant/.config/market-data-center/env",
        "/tmp/second.env",
    ]


def test_repository_templates_bind_only_the_machine_level_config_directory() -> None:
    assert unexpected_templates(ROOT / "deploy/systemd") == []


def test_templates_sourcing_a_checkout_are_reported(tmp_path: Path) -> None:
    (tmp_path / "good.service").write_text("[Service]\nEnvironmentFile=-%h/.config/market-data-center/env\n")
    (tmp_path / "drifted.service").write_text(
        "[Service]\nEnvironmentFile=/home/quant/repos/market-data-center-latest/.env.local\n"
    )

    findings = unexpected_templates(tmp_path)

    assert findings == ["drifted.service: EnvironmentFile=/home/quant/repos/market-data-center-latest/.env.local"]


def test_installed_drop_ins_sourcing_a_checkout_are_reported(tmp_path: Path) -> None:
    unit_dir = tmp_path / "systemd"
    drop_in_dir = unit_dir / "market-data-center-api.service.d"
    drop_in_dir.mkdir(parents=True)
    (drop_in_dir / "provider-env.conf").write_text(
        "[Service]\nEnvironmentFile=/home/quant/repos/market-data-center-latest/.env.local\n"
    )
    config_dir = tmp_path / "market-data-center"

    expected = (
        f"{drop_in_dir / 'provider-env.conf'}: "
        "EnvironmentFile=/home/quant/repos/market-data-center-latest/.env.local"
    )

    assert unexpected_installed(unit_dir, config_dir) == [expected]


def test_installed_units_using_the_machine_level_config_directory_pass(tmp_path: Path) -> None:
    unit_dir = tmp_path / "systemd"
    unit_dir.mkdir()
    config_dir = Path.home() / ".config/market-data-center"
    (unit_dir / "market-data-center-api.service").write_text(
        "[Service]\nEnvironmentFile=-%h/.config/market-data-center/env\n"
    )
    (unit_dir / "market-data-center-api.service.d").mkdir()
    (unit_dir / "market-data-center-api.service.d" / "provider-env.conf").write_text(
        f"[Service]\nEnvironmentFile={config_dir / 'env'}\n"
    )
    (unit_dir / "unrelated.service").write_text("[Service]\nEnvironmentFile=/etc/other.env\n")

    assert unexpected_installed(unit_dir, config_dir) == []
