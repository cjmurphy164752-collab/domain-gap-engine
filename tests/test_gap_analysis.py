from __future__ import annotations

import copy
import json

import pytest

from opportunity_engine import gap_analysis as ga


def observations():
    return [
        {
            "id": "O1",
            "kind": "result",
            "field": "reported_incidents.0.Name",
            "quote": "The latch opened during use.",
        }
    ]


def candidate():
    claim = {"text": "The latch opened during use.", "evidence_ids": ["O1"]}
    gap = {
        "title": "Latch opens during use",
        **{key: copy.deepcopy(claim) for key in ga.CLAIMS},
        "competing_explanations": ["Was the latch fully engaged?"],
        "open_questions": ["Did the remedy resolve the reported issue?"],
        "aspects": {
            key: {"value": "unknown", "rationale": {"text": "unknown", "evidence_ids": []}}
            for key in ga.ASPECTS
        },
    }
    gap["aspects"]["evidence_grounding"] = {"value": "tentative", "rationale": claim}
    return gap


def test_quotes_must_be_verbatim_in_named_field():
    extracted = {"observations": observations()}
    fields = {"reported_incidents.0.Name": "The latch opened during use."}
    assert ga.validate_observations(extracted, fields)
    extracted["observations"][0]["quote"] = "The latch opened and caused injuries."
    with pytest.raises(ValueError, match="verbatim"):
        ga.validate_observations(extracted, fields)


def test_unknown_claims_are_allowed_but_factual_claims_need_ids():
    gap = candidate()
    gap["intent"] = {"text": "unknown", "evidence_ids": []}
    ga.validate_gap(gap, observations())
    gap["intent"]["text"] = "The user wants to buy a better latch"
    with pytest.raises(ValueError, match="Unsupported"):
        ga.validate_gap(gap, observations())


@pytest.mark.parametrize(
    "mutation",
    [
        lambda g: g.update(product_wedge="new product"),
        lambda g: g["result"].update(evidence_ids=["invented"]),
        lambda g: g["aspects"]["evidence_grounding"].update(value="corroborated"),
        lambda g: g["aspects"]["blocker_understanding"].update(value="tested"),
        lambda g: g.update(title="We should build a new latch"),
    ],
)
def test_reject_solution_and_unsupported_strength(mutation):
    gap = candidate()
    mutation(gap)
    with pytest.raises(ValueError):
        ga.validate_gap(gap, observations())


def test_model_review_can_reject_cited_but_unentailed_claims():
    record = {
        "id": "test",
        "url": "https://example.org/test",
        "reported_incidents": [{"Name": "The latch opened during use."}],
    }
    replies = iter(
        [
            {"observations": observations()},
            {"gaps": [candidate()]},
            {
                "reviews": [
                    {
                        "index": 0,
                        "retain": False,
                        "reason": "Intent and consequence not established",
                    }
                ]
            },
        ]
    )
    result = ga.analyze_one(record, {}, lambda *args: next(replies))
    assert result["gaps"] == []
    assert len(result["proposed_gaps"]) == 1


def test_empty_result_valid_and_metadata_not_analyzed(tmp_path):
    calls = []
    records = [{"source": "GitHub/x/y"}]
    result = ga.analyze_records(
        records, {"model": "test"}, tmp_path, lambda *args: calls.append(args)
    )
    assert not calls and result["skipped_metadata_only"] == 1
    assert result["gaps"] == []


def test_pending_budget_and_cached_negative_not_confused(tmp_path):
    records = [
        {"source": "CPSC", "id": str(i), "url": "https://example.org", "description": str(i)}
        for i in range(2)
    ]

    def empty(_settings, _prompt, _data, schema):
        return {next(iter(schema["properties"])): []}

    settings = {"model": "test", "max_records_per_cycle": 1}
    first = ga.analyze_records(records, settings, tmp_path, empty)
    assert first["status"] == "partial" and first["pending"] == 1
    second = ga.analyze_records(records, settings, tmp_path, empty)
    assert second["status"] == "complete" and second["processed"] == 2


def test_incomplete_critique_cannot_pass():
    replies = iter([{"observations": observations()}, {"gaps": [candidate()]}, {"reviews": []}])
    with pytest.raises(ValueError, match="Incomplete"):
        ga.analyze_one(
            {"reported_incidents": [{"Name": "The latch opened during use."}]},
            {},
            lambda *args: next(replies),
        )


def test_empty_proposals_need_no_critique():
    replies = iter([{"observations": []}, {"gaps": []}])
    result = ga.analyze_one({}, {}, lambda *args: next(replies))
    assert result["reviews"] == [] and result["gaps"] == []


@pytest.mark.parametrize("index", [False, 0.0, "0"])
def test_review_index_must_be_integer(index):
    replies = iter(
        [
            {"observations": observations()},
            {"gaps": [candidate()]},
            {"reviews": [{"index": index, "retain": True, "reason": "Passed"}]},
        ]
    )
    with pytest.raises(ValueError, match="review index"):
        ga.analyze_one(
            {"reported_incidents": [{"Name": "The latch opened during use."}]},
            {},
            lambda *args: next(replies),
        )


def test_corrupt_cache_and_retry_counter_are_recoverable(tmp_path):
    records = [{"source": "CPSC", "id": "1", "url": "https://example.org"}]
    settings = {"model": "test"}

    def empty(_settings, _prompt, _data, schema):
        return {next(iter(schema["properties"])): []}

    ga.analyze_records(records, settings, tmp_path, empty)
    path = next((tmp_path / "gap-analysis").glob("*.json"))
    path.write_text('{"incomplete":')
    path.with_suffix(".error.json").write_text('{"attempts": "invalid"}')
    result = ga.analyze_records(records, settings, tmp_path, empty)
    assert result["processed"] == 1 and result["pending"] == 0
    assert path.with_suffix(".corrupt.json").exists()
    assert json.loads(path.read_text())["source_id"] == "1"


def test_changed_source_date_invalidates_cached_analysis(tmp_path):
    record = {"source": "CPSC", "id": "1", "url": "https://example.org", "published": "2026-01-01"}
    calls = []

    def empty(_settings, _prompt, _data, schema):
        calls.append(schema)
        return {next(iter(schema["properties"])): []}

    ga.analyze_records([record], {"model": "test"}, tmp_path, empty)
    assert len(calls) == 2
    record["published"] = "2026-02-01"
    ga.analyze_records([record], {"model": "test"}, tmp_path, empty)
    assert len(calls) == 4


def test_incomplete_cached_render_metadata_is_quarantined(tmp_path):
    record = {
        "source": "CPSC",
        "id": "1",
        "url": "https://example.org",
        "reported_incidents": [{"Name": "The latch opened during use."}],
    }
    replies = iter(
        [
            {"observations": observations()},
            {"gaps": [candidate()]},
            {"reviews": [{"index": 0, "retain": True, "reason": "Test verdict"}]},
        ]
    )
    ga.analyze_records([record], {"model": "test"}, tmp_path, lambda *args: next(replies))
    path = next((tmp_path / "gap-analysis").glob("*.json"))
    cached = json.loads(path.read_text())
    del cached["gaps"][0]["review_reason"]
    path.write_text(json.dumps(cached))
    result = ga.analyze_records(
        [record],
        {"model": "test", "max_records_per_cycle": 0},
        tmp_path,
    )
    assert result["pending"] == 1 and result["gaps"] == []
    assert path.with_suffix(".corrupt.json").exists()


def test_combined_model_input_limit_prevents_process_launch(monkeypatch):
    monkeypatch.setattr(
        ga.urllib.request, "urlopen", lambda *a, **k: pytest.fail("Must not launch")
    )
    with pytest.raises(ValueError, match="byte budget"):
        ga.model_call({"model": "test"}, "review", {"evidence": "🏋" * 4000}, ga.GAPS)


def test_collection_http_budget_prevents_retry_sleep(monkeypatch):
    import time

    from opportunity_engine.http import HttpClient

    client = HttpClient(user_agent="test", deadline=time.monotonic() + 1)
    monkeypatch.setattr(time, "sleep", lambda *a: pytest.fail("Must not wait past deadline"))
    with pytest.raises(TimeoutError, match="deadline"):
        client._pause(60)
