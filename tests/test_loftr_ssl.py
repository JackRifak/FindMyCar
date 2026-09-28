import ssl

import certifi

from fmc.vpr.verifiers import loftr_verifier as mod


def test_ensure_certifi_https_context_loads_certifi_bundle(monkeypatch):
    captured = {}

    class DummyContext:
        pass

    def fake_create_default_context(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return DummyContext()

    monkeypatch.setattr(ssl, "create_default_context", fake_create_default_context)
    mod._ensure_certifi_https_context()
    ssl._create_default_https_context()

    assert captured["kwargs"]["cafile"] == certifi.where()
