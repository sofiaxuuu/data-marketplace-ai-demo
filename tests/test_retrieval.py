from copy import deepcopy

import httpx
import pytest

from datascout import embeddings
from datascout.catalog import catalog
from datascout.retrieval import content_hash, document


def test_embedding_batch_is_normalized_and_restored_to_input_order(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-only-key")
    vectors = [[2.0] + [0.0] * 1535, [0.0, 3.0] + [0.0] * 1534]
    real_client = httpx.Client

    def response(request):
        return httpx.Response(200, json={"data": [
            {"index": 1, "embedding": vectors[1]},
            {"index": 0, "embedding": vectors[0]},
        ]})

    monkeypatch.setattr(embeddings.httpx, "Client", lambda **kwargs: real_client(transport=httpx.MockTransport(response), **kwargs))
    result = embeddings.embed(["first", "second"])
    assert result[0][0] == 1.0
    assert result[1][1] == 1.0


def test_invalid_embedding_is_rejected():
    with pytest.raises(embeddings.EmbeddingError):
        embeddings.normalized([float("nan")] * 1536)


def test_document_excludes_paths_and_changes_invalidate_hash():
    item = catalog()[0]
    text = document(item)
    assert item["tables"][0]["path"] not in text
    changed = deepcopy(item)
    changed["description"] += " Updated definition."
    assert content_hash(changed) != content_hash(item)


def test_stale_index_version_is_excluded(monkeypatch):
    from datascout import retrieval

    item = catalog()[0]
    rows = [
        (item["id"], item["version"] + 1, item["name"], content_hash(item), 0.99),
        (item["id"], item["version"], item["name"], content_hash(item), 0.75),
    ]

    class Cursor:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def execute(self, *args): pass
        def fetchall(self): return rows

    class Connection:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def cursor(self): return Cursor()

    monkeypatch.setattr(retrieval, "connect", Connection)
    monkeypatch.setattr(retrieval, "embed", lambda texts: [[1.0] + [0.0] * 1535])
    result = retrieval.search("An example question")
    assert len(result) == 1
    assert result[0]["version"] == item["version"]
    assert result[0]["score"] == 0.75
