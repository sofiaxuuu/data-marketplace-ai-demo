from evals import source_advisor as evaluation
from datascout import source_advice
from datascout.catalog import catalog
from datascout.inspection import public_product


def test_evaluation_only_includes_development_and_defaults_offline(monkeypatch):
    cases = evaluation.development_cases()
    assert cases and all(c.split == "development" for c in cases)
    monkeypatch.setattr(evaluation, "advise", lambda *args: (_ for _ in ()).throw(AssertionError("No live call")))
    assert evaluation.main([]) == 0


def test_evaluation_scores_source_choice_separately(monkeypatch):
    case = next(c for c in evaluation.development_cases() if c.expected_outcome == "select")
    metadata = [public_product(p) for p in catalog()]
    p = next(p for p in metadata if p["id"] == case.expected_data_products[0])
    advice = source_advice.Advice(outcome="recommend", limitation="none", reason="Fields fit", clarification="",
        recommendations=[source_advice.Recommendation(product_id=p["id"], manifest_version=p["version"], reason="Coverage fits", caveats=[])])
    monkeypatch.setattr(evaluation, "advise", lambda *args: (advice, "fixture", {}))
    row = evaluation.evaluate(case, metadata)
    assert row["outcome_correct"] and row["top_choice_correct"]
