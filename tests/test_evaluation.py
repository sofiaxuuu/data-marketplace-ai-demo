"""Evaluation metrics/labels are tested independently of production heuristics."""

from copy import deepcopy

import pytest
from pydantic import ValidationError

from evals import run
from datascout.catalog import catalog


def test_benchmark_is_balanced_and_split_with_attribution():
    cases = run.load_cases()
    assert len(cases) == 36
    assert sum(case.expected_outcome == "select" for case in cases) == 18
    assert sum(case.split == "held_out" for case in cases) == 12
    assert sum(case.origin.kind == "finsearchcomp" for case in cases) == 6
    assert all(case.origin.source_id for case in cases if case.origin.kind == "finsearchcomp")


def test_all_numeric_labels_match_frozen_snapshots():
    assert run.verify_ground_truth(run.load_cases())["numeric_gold_verified"] == 18


def test_changed_data_requires_gold_review():
    products = deepcopy(catalog())
    products[0]["snapshot"]["snapshot_sha256"] = "changed"
    with pytest.raises(ValueError, match="changed"):
        run.verify_ground_truth(run.load_cases(), products)


def test_wrong_gold_value_is_rejected():
    case = run.load_cases()[0].model_copy(deep=True)
    case.gold.value = 999
    with pytest.raises(ValueError, match="gold"):
        run.verify_ground_truth([case])


def test_inconsistent_abstention_label_is_rejected():
    payload = run.load_cases()[0].model_dump()
    payload["expected_outcome"] = "abstain"
    with pytest.raises(ValidationError):
        run.Case.model_validate(payload)


def test_ranking_and_selection_are_scored_separately():
    case = run.load_cases()[0]
    proposal = {"outcome": "abstain", "retrieved_products": [
        {"id": "world_bank_us_gdp_per_capita", "score": 0.8},
        {"id": "fred_unemployment", "score": 0.7},
    ]}
    result = run.score_case(case, proposal, use_retrieval=True)
    result["preview_ms"] = 10
    metrics = run.summarize([result])
    assert metrics["recall_at_1"] == 0
    assert metrics["recall_at_3"] == 1
    assert metrics["mrr_at_3"] == 0.5
    assert metrics["selection_accuracy"] == 0


def test_local_mode_does_not_claim_retrieval_accuracy():
    case = run.load_cases()[0]
    result = run.score_case(case, {"outcome": "selected", "product": {"id": "fred_unemployment"}}, use_retrieval=False)
    result["preview_ms"] = 10
    metrics = run.summarize([result])
    assert metrics["selection_accuracy"] == 1
    assert metrics["recall_at_1"] is None
    assert metrics["mrr_at_3"] is None


def test_blocked_answers_remain_in_numeric_accuracy_denominator(monkeypatch):
    monkeypatch.setattr(run, "preview", lambda *args, **kwargs: {"outcome": "abstain"})
    result = run.run_case(run.load_cases()[0], mode="local", run_execution=True)
    assert result["numeric_correct"] is False
    metrics = run.summarize([result])
    assert metrics["numeric_task_accuracy"] == 0
    assert metrics["returned_row_accuracy"] is None
    assert "execution_blocked_by_selection" in result["failure_categories"]


def test_numeric_rows_require_correct_period_product_and_value():
    case = run.load_cases()[0]
    answer = {"product": {"id": "fred_unemployment"}, "rows": [{"observation_date": "2018-01-01", "unemployment_rate": 4.0}]}
    assert run.rows_correct(case, answer)
    answer["rows"][0]["observation_date"] = "2019-01-01"
    assert not run.rows_correct(case, answer)
    answer["rows"][0]["observation_date"] = "2018-01-01"
    answer["product"]["id"] = "wrong_product"
    assert not run.rows_correct(case, answer)


def test_provider_failure_is_not_counted_as_correct_abstention(monkeypatch):
    case = next(case for case in run.load_cases() if case.expected_outcome == "abstain")

    def fail(*args, **kwargs):
        raise RuntimeError("private-test-key")

    monkeypatch.setattr(run, "preview", fail)
    result = run.run_case(case, mode="singlestore", run_execution=False)
    assert not result["selection_correct"]
    assert "private-test-key" not in str(result)
    assert run.summarize([result])["abstention_accuracy"] == 0


def test_provisional_checks_are_unmeasured_when_metric_is_absent():
    result = run.criteria_checks({"recall_at_1": None}, {"targets": {"recall_at_1": {"minimum": 0.9}}})
    assert result["recall_at_1"]["passed"] is None
