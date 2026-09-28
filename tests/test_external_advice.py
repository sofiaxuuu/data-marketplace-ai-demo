"""External source recommendations remain grounded in returned search evidence."""
import pytest

from datascout import external_advice


def test_external_recommendation_requires_grounded_candidate(monkeypatch):
    candidates = [{"title": "Daily measurements", "publisher": "example.org",
                   "url": "https://example.org/daily_2024.csv", "evidence": "Daily PM2.5 for 2024"}]
    assert external_advice.recommend("Question", [])[0]["outcome"] == "insufficient_evidence"
    monkeypatch.setenv("OPENAI_API_KEY", "test")

    class Response:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"status": "completed", "output": [{"type": "message", "content": [
                {"type": "output_text", "text": '{"outcome":"recommend","primary_index":4,"assessments":[{"candidate_index":4,"fit":"strong","reason":"Daily data","caveat":"Verify units"}],"unresolved":[]}'}]}]}

    class Client:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def post(self, *args, **kwargs):
            return Response()

    monkeypatch.setattr(external_advice.httpx, "Client", Client)
    with pytest.raises(external_advice.ExternalAdviceError, match="invalid structured evidence"):
        external_advice.recommend("Question", candidates)

    def valid_json(self):
        return {"status": "completed", "output": [{"type": "message", "content": [
            {"type": "output_text", "text": '{"outcome":"recommend","primary_index":0,"assessments":[{"candidate_index":0,"fit":"strong","reason":"Daily data","caveat":"Verify units"}],"unresolved":[]}'}]}]}

    monkeypatch.setattr(Response, "json", valid_json)
    result, _, _ = external_advice.recommend("Question", candidates)
    assert result["assessments"][0]["evidence_quote"] == candidates[0]["evidence"]
    assert "original documentation" in result["answer"]
