from pytest import MonkeyPatch

from robust_rag.core.settings import Settings


def test_settings_defaults_are_local_only() -> None:
    settings = Settings(_env_file=None)

    assert settings.api_host == "127.0.0.1"
    assert settings.redis_url.startswith("redis://127.0.0.1")
    assert settings.llm_base_url == "http://127.0.0.1:15721/v1"
    assert settings.llm_model == "gpt-5.6-luna"


def test_settings_can_be_overridden(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("API_PORT", "9000")

    settings = Settings(_env_file=None)

    assert settings.app_env == "test"
    assert settings.api_port == 9000
