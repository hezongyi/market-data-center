import importlib.util
from pathlib import Path

_MODULE_PATH = Path(__file__).parents[2] / "scripts" / "secret_scan.py"
_SPEC = importlib.util.spec_from_file_location("secret_scan", _MODULE_PATH)
assert _SPEC and _SPEC.loader
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
has_secret = _MODULE.has_secret


def test_secret_scan_detects_credential_shapes() -> None:
    assert has_secret(b"DATACENTER_API_KEY" + b'="' + b"a-secure-value-123" + b'"')
    assert has_secret(b"FRED_API_KEY" + b" = '" + b"abc12345" + b"'")
    assert has_secret(b"-----" + b"BEGIN " + b"PRIVATE KEY-----")
    assert has_secret(b"ghp_" + b"a" * 20)
    assert has_secret(b"github_pat_" + b"a" * 20)
    assert has_secret(b"sk-" + b"a" * 20)
    assert has_secret(b"xoxb-" + b"a" * 20)


def test_secret_scan_ignores_lookup_source_and_short_prefixes() -> None:
    repo_root = Path(__file__).parents[2]
    browser_lookup = (repo_root / "scripts/browser_acceptance.cjs").read_bytes()
    scanner_source = (repo_root / "scripts/secret_scan.py").read_bytes()

    assert not has_secret(browser_lookup)
    assert not has_secret(scanner_source)
    assert not has_secret(b"DATACENTER_API_KEY" + b'="')
    assert not has_secret(b"ghp" + b"_")
