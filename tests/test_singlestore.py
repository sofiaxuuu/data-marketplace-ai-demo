import pytest

from datascout.singlestore import ConfigurationError, Settings, connect, connection_error_message


VALUES = {
    "SINGLESTORE_HOST": "example.test",
    "SINGLESTORE_PORT": "3333",
    "SINGLESTORE_DATABASE": "datascout",
    "SINGLESTORE_USER": "user with spaces",
    "SINGLESTORE_PASSWORD": "test-only-@:#/password",
}


def test_credentials_are_separate_arguments_and_tls_is_verified(monkeypatch):
    captured = {}

    def fake_connect(**kwargs):
        captured.update(kwargs)
        return "connection"

    monkeypatch.setattr("datascout.singlestore.s2.connect", fake_connect)
    settings = Settings.from_env(VALUES)
    assert connect(settings) == "connection"
    assert captured["user"] == VALUES["SINGLESTORE_USER"]
    assert captured["password"] == VALUES["SINGLESTORE_PASSWORD"]
    assert captured["ssl_verify_cert"] is True
    assert captured["ssl_verify_identity"] is True
    assert captured["ssl_ca"]
    assert VALUES["SINGLESTORE_PASSWORD"] not in repr(settings)


def test_missing_password_is_reported_before_connecting():
    with pytest.raises(ConfigurationError, match="SINGLESTORE_PASSWORD"):
        Settings.from_env({**VALUES, "SINGLESTORE_PASSWORD": ""})


def test_error_classification_does_not_expose_driver_details():
    error = Exception(1045, "Access denied: secret-password@example.test")
    message = connection_error_message(error)
    assert "Authentication rejected" in message
    assert "secret-password" not in message


def test_custom_certificate_path_is_used(monkeypatch, tmp_path):
    bundle = tmp_path / "ca.pem"
    bundle.write_text("test fixture")
    settings = Settings.from_env({**VALUES, "SINGLESTORE_SSL_CA": str(bundle)})
    captured = {}
    monkeypatch.setattr("datascout.singlestore.s2.connect", lambda **kwargs: captured.update(kwargs))
    connect(settings)
    assert captured["ssl_ca"] == str(bundle)
