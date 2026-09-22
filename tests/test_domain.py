import json

import pytest

from opportunity_engine.domain_cli import initialize
from opportunity_engine.local_evidence import read_evidence


def test_domain_setup_generates_inactive_workflows_without_credentials(tmp_path):
    directory = initialize(tmp_path / "domain", "CAD import reliability", port=5692)
    config = json.loads((directory / "config.local.json").read_text())
    assert config["domain"] == "CAD import reliability"
    assert not config["gap_analysis"]["enabled"]
    assert config["cpsc_products"] == []
    flows = json.loads((directory / "n8n-workflows.local.json").read_text())
    assert len(flows) == 3 and not any(x["active"] for x in flows)
    credential = json.loads((directory / "delivery.local.json").read_text())
    assert credential["secret"] not in json.dumps(flows)
    assert all("credentials" not in n for f in flows for n in f["nodes"])
    assert "5692/collect" in json.dumps(flows)
    with pytest.raises(FileExistsError):
        initialize(directory, "Do not overwrite")


def test_import_requires_rights_and_retains_counterevidence(tmp_path):
    path = tmp_path / "input.jsonl"
    item = {
        "id": "one",
        "title": "Synthetic test",
        "url": "https://example.org/test",
        "evidence": {"result": "Import failed", "counterevidence": "Retry succeeded"},
    }
    path.write_text(json.dumps(item))
    with pytest.raises(ValueError, match="rights"):
        read_evidence(path)
    item["rights"] = {
        "local_analysis_allowed": True,
        "license": "user-authored",
        "permission_reference": "Synthetic test author",
    }
    path.write_text(json.dumps(item))
    result = read_evidence(path)[0]
    assert result["evidence"]["counterevidence"] == "Retry succeeded"
    assert result["source"] == "AuthorizedLocalEvidence"


def test_model_is_loopback_only_and_no_bridge_execution():
    import inspect

    from opportunity_engine import gap_analysis

    source = inspect.getsource(gap_analysis.model_call)
    assert "http://127.0.0.1:11434/api/chat" in source
    assert "subprocess" not in source


def test_local_evidence_reaches_gap_lane_and_report(tmp_path):
    from opportunity_engine.horizon import collect

    directory = initialize(tmp_path / "software", "import reliability", model="test-model")
    config = json.loads((directory / "config.local.json").read_text())
    config["crossref_queries"] = []
    config["gap_analysis"]["max_records_per_cycle"] = 0
    item = {
        "id": "one",
        "title": "Synthetic import test",
        "url": "https://example.org/test",
        "evidence": {"result": "Import failed"},
        "rights": {
            "local_analysis_allowed": True,
            "license": "user-authored",
            "permission_reference": "Test fixture",
        },
    }
    (directory / "evidence.local.jsonl").write_text(json.dumps(item))
    report = collect(config)
    assert report["analysis"]["eligible"] == 1
    assert report["analysis"]["pending"] == 1
    assert report["observations"][0]["source"] == "AuthorizedLocalEvidence"
    assert report["domain"] == "import reliability"
    item["rights"]["local_analysis_allowed"] = False
    (directory / "evidence.local.jsonl").write_text(json.dumps(item))
    revoked = collect(config)
    assert revoked["status"] == "failed"
    assert revoked["gap_analysis"] == "not_run"
    assert revoked["historical_analysis_records"] == 0
