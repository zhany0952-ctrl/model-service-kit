import json
from pathlib import Path

from tools.release_audit import audit, candidate_files

ROOT = Path(__file__).resolve().parents[1]


def test_launcher_uses_only_explicit_adjacent_or_path_interpreter():
    source = (ROOT / "open-model-config.cmd").read_text("utf-8")
    assert source == (ROOT / "distribution/open-model-config.cmd").read_text("utf-8")
    assert "MODEL_SERVICE_PYTHON" in source and ".venv\\Scripts\\python.exe" in source
    assert "..\\" not in source


def test_audit_export_does_not_include_local_configuration_or_generated_files(tmp_path):
    (tmp_path / "README.md").write_text("public", "utf-8")
    for folder, name in [
        ("data", "profiles.json"),
        (".venv", "private.py"),
        ("build", "copy.py"),
        ("src/__pycache__", "copy.py"),
    ]:
        target = tmp_path / folder / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("private", "utf-8")
    assert [p.relative_to(tmp_path).as_posix() for p in candidate_files(tmp_path)] == ["README.md"]


def test_audit_reports_location_without_printing_sensitive_match(tmp_path):
    value = "sk-" + "a" * 40
    (tmp_path / "README.md").write_text(value, "utf-8")
    report = audit(tmp_path)
    assert any(f["code"] == "provider_token" for f in report["findings"])
    assert value not in json.dumps(report)
    assert report["requires_human_confirmation"] is True


def test_no_runtime_business_imports_and_library_remains_separately_versioned():
    for path in (ROOT / "src/model_service").glob("*.py"):
        code = path.read_text("utf-8")
        assert "from npc_agent" not in code and "import npc_agent" not in code
