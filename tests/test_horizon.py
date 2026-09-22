from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from opportunity_engine import horizon


class Sources:
    def __init__(self, fail=False):
        self.fail = fail

    def get_json(self, url, *, params):
        if self.fail:
            raise RuntimeError("offline")
        if "saferproducts" in url:
            result = [
                {
                    "RecallID": 1,
                    "Title": "Test treadmill notice",
                    "URL": "https://www.cpsc.gov/Recalls/test",
                    "Hazards": [{"Name": "Test"}],
                    "Injuries": [{"Name": "No incidents reported"}],
                    "Remedies": [{"Name": "Existing remedy"}],
                }
            ]
        else:
            result = [
                {
                    "id": 2,
                    "name": "v1",
                    "tag_name": "v1",
                    "draft": False,
                    "html_url": "https://github.com/test/test/releases/tag/v1",
                    "body": "Do not retain this body",
                    "author": {"login": "private"},
                }
            ]
        return result, json.dumps(result).encode()


@pytest.fixture
def config(tmp_path):
    return {
        "root": str(tmp_path),
        "cpsc_products": ["treadmill", "treadmill duplicate"],
        "github_repositories": ["test/test"],
    }


def test_source_provenance_dedup_and_no_invented_gaps(config, tmp_path):
    first = horizon.collect(config, client=Sources())
    assert len(first["observations"]) == 2
    assert all(x["change"] == "new" for x in first["observations"])
    assert first["gaps"] is None
    second = horizon.collect(config, client=Sources())
    assert all(x["change"] == "unchanged" for x in second["observations"])
    recall = first["observations"][0]
    assert recall["reported_remedies"] and recall["reported_incidents"]
    assert recall["intent"] == "unknown"
    for path in (tmp_path / "raw" / "github").rglob("*.json"):
        assert "Do not retain" not in path.read_text()
        assert "private" not in path.read_text()


def test_total_failure_is_not_zero_gaps(config):
    report = horizon.collect(config, client=Sources(fail=True))
    assert report["status"] == "failed"
    assert report["gap_analysis"] == "not_run"
    assert "not a finding of zero gaps" in horizon.digest(report)["digest"]


def test_receipt_blocks_duplicates_and_uncertain_retries(config, monkeypatch):
    horizon.collect(config, client=Sources())
    requests = []

    class Reply:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self):
            p = json.loads(requests[-1].data)
            return json.dumps(
                {
                    "report_id": p["report_id"],
                    "status": "telegram_accepted",
                    "telegram_message_id": 7,
                }
            ).encode()

    def send(request, **kwargs):
        requests.append(request)
        return Reply()

    monkeypatch.setattr(horizon.urllib.request, "urlopen", send)
    credential = {"url": "http://localhost/test", "header": "X-Research-Token", "secret": "test"}
    assert horizon.dispatch(config, credential)["telegram_message_id"] == 7
    assert horizon.dispatch(config, credential)["status"] == "already_delivered"
    assert len(requests) == 1
    horizon.collect(config, client=Sources())

    def timeout(*args, **kwargs):
        raise TimeoutError()

    monkeypatch.setattr(horizon.urllib.request, "urlopen", timeout)
    with pytest.raises(TimeoutError):
        horizon.dispatch(config, credential)
    with pytest.raises(RuntimeError, match="uncertain"):
        horizon.dispatch(config, credential)


def test_stale_report_yields_missing_research_alert(config, monkeypatch):
    horizon.collect(config, client=Sources())
    later = horizon.now() + timedelta(days=2)
    monkeypatch.setattr(horizon, "now", lambda: later)
    captured = []

    def stop(request, **kwargs):
        captured.append(json.loads(request.data))
        raise TimeoutError()

    monkeypatch.setattr(horizon.urllib.request, "urlopen", stop)
    with pytest.raises(TimeoutError):
        horizon.dispatch(config, {"url": "http://localhost/test", "header": "X", "secret": "x"})
    assert captured[0]["status"] == "failed"
    assert captured[0]["report_id"].startswith("missing-research-")


def test_crossref_metadata_only_normalization():
    record = horizon.normalize_crossref(
        {
            "DOI": "10.1000/TEST",
            "title": ["Sensor validation"],
            "published": {"date-parts": [[2026, 9, 21]]},
            "abstract": "Do not retain",
            "author": [{"name": "private"}],
        }
    )
    assert record["id"] == "doi:10.1000/test"
    assert record["published"] == "2026-09-21"
    assert "abstract" not in record and "author" not in record


def test_digest_keeps_limitations_with_many_unicode_candidates():
    report = {
        "report_id": "test",
        "status": "partial",
        "finished_at": "2026-09-21",
        "observations": [],
        "coverage": [],
        "analysis": {"processed": 30, "eligible": 40, "pending": 10},
        "gaps": [
            {"title": "🏋" * 200, "source_url": "https://example.org/" + "a" * 300}
            for _ in range(10)
        ],
    }
    result = horizon.digest(report)
    assert len(result["digest"].encode("utf-16-le")) // 2 <= 3200
    assert "Reviews and owner experiences are not connected" in result["digest"]
    assert "10 tentative candidates" in result["digest"]


def test_failed_collection_does_not_claim_completed_gap_review(config, monkeypatch):
    config["gap_analysis"] = {"enabled": True}
    monkeypatch.setattr(horizon, "analyze_records", lambda *a: pytest.fail("No input to analyze"))
    report = horizon.collect(config, client=Sources(fail=True))
    assert report["status"] == "failed" and report["gap_analysis"] == "not_run"


def test_inventory_and_pending_report_survive_gpu_interruption(config, monkeypatch):
    config["gap_analysis"] = {"enabled": True}

    def interrupt(*args):
        raise KeyboardInterrupt()

    monkeypatch.setattr(horizon, "analyze_records", interrupt)
    with pytest.raises(KeyboardInterrupt):
        horizon.collect(config, client=Sources())
    root = Path(config["root"])
    with sqlite3.connect(root / "horizon.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 2
    pointer = horizon.read_json(root / "latest.json")
    report = horizon.read_json(Path(pointer["path"]))
    assert report["status"] == "partial" and report["gap_analysis"] == "pending"
    assert len(report["observations"]) == 2


def test_digest_counts_items_hidden_by_length_budget():
    report = {
        "report_id": "test",
        "status": "complete",
        "finished_at": "2026-09-21",
        "coverage": [],
        "observations": [
            {"change": "new", "source": "x" * 2400, "title": "test", "url": "https://example.org"}
            for _ in range(3)
        ],
    }
    assert "3 additional changes" in horizon.digest(report)["digest"]


def test_backlog_survives_missing_source_in_next_scan(config, monkeypatch):
    horizon.collect(config, client=Sources())
    config["gap_analysis"] = {"enabled": True, "model": "test", "max_records_per_cycle": 0}
    report = horizon.collect(config, client=Sources(fail=True))
    assert report["status"] == "failed"
    assert report["historical_analysis_records"] == 1
    assert report["analysis"]["eligible"] == 1 and report["analysis"]["pending"] == 1
    assert report["observations"] == []


def test_expired_collection_budget_defers_without_fetching(config):
    config["collection_max_seconds"] = 0

    class Never:
        def get_json(self, *args, **kwargs):
            pytest.fail("Expired run must not fetch")

    report = horizon.collect(config, client=Never())
    assert len(report["coverage"]) == 3
    assert all("deferred" in x["error"] for x in report["coverage"])
    cursor = horizon.read_json(Path(config["root"]) / "collection-cursor.json")
    assert cursor["next"] == ["cpsc", "treadmill"]


def test_saved_cursor_resumes_deferred_source_first(config):
    horizon.write_json(
        Path(config["root"]) / "collection-cursor.json", {"next": ["github", "test/test"]}
    )
    report = horizon.collect(config, client=Sources())
    assert report["coverage"][0]["source"] == "github"
