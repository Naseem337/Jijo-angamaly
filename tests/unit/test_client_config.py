import shutil
from pathlib import Path

import pytest
import yaml

from app.core.client_config import ConfigError, load_all_clients, load_client_config
from app.settings import EnvSecretResolver, SecretNotFound, secret_name_to_env_var


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    monkeypatch.setenv("TRUECHOICE_STAFF_NUMBERS", "9847012345, +91 94470 00000")
    monkeypatch.setenv("TRUECHOICE_WHATSAPP_PHONE_NUMBER_ID", "123456789")
    monkeypatch.setenv("ANTHROPIC_MODEL", "")
    return monkeypatch


def test_truechoice_config_loads(env: pytest.MonkeyPatch, clients_dir: Path) -> None:
    cfg = load_client_config("truechoice", clients_dir)
    assert cfg.display_name == "True Choice"
    assert cfg.whatsapp.phone_number_id == "123456789"
    assert cfg.staff_numbers == ["+919847012345", "+919447000000"]
    assert cfg.is_staff("+919847012345")
    assert cfg.providers.llm_chain == ["deepseek", "openrouter", "anthropic"]
    assert cfg.providers.llm["deepseek"].model == "deepseek-chat"
    assert cfg.voice.policy == "mirror"
    rules = cfg.business_rules
    assert rules.retention_days == 90
    assert rules.bot.auto_resume_hours == 24
    assert rules.alerts.max_cars_per_message == 3
    assert rules.alerts.near_match.budget_over_pct == 10


def test_claude_model_is_not_hardcoded(env: pytest.MonkeyPatch, clients_dir: Path) -> None:
    """The Anthropic model comes only from ANTHROPIC_MODEL; empty means 'not configured'."""
    cfg = load_client_config("truechoice", clients_dir)
    assert cfg.providers.llm["anthropic"].model is None
    env.setenv("ANTHROPIC_MODEL", "my-haiku-id")
    cfg = load_client_config("truechoice", clients_dir)
    assert cfg.providers.llm["anthropic"].model == "my-haiku-id"
    text = (clients_dir / "truechoice" / "client.yaml").read_text(encoding="utf-8")
    assert "claude-" not in text


def test_load_all_clients(env: pytest.MonkeyPatch, clients_dir: Path) -> None:
    assert "truechoice" in load_all_clients(clients_dir)


def _copy_client(tmp_path: Path, clients_dir: Path) -> Path:
    shutil.copytree(clients_dir / "truechoice", tmp_path / "truechoice")
    return tmp_path / "truechoice" / "client.yaml"


def _edit_yaml(path: Path, mutate) -> None:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    mutate(data)
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")


def test_unknown_provider_in_chain_is_rejected(env, clients_dir: Path, tmp_path: Path) -> None:
    path = _copy_client(tmp_path, clients_dir)
    _edit_yaml(path, lambda d: d["providers"]["llm_chain"].append("nope"))
    with pytest.raises(ConfigError, match="undefined providers"):
        load_client_config("truechoice", tmp_path)


def test_invalid_staff_number_is_rejected(env, clients_dir: Path, tmp_path: Path) -> None:
    env.setenv("TRUECHOICE_STAFF_NUMBERS", "12345")
    with pytest.raises(ConfigError):
        load_client_config("truechoice", clients_dir)


def test_client_id_must_match_folder(env, clients_dir: Path, tmp_path: Path) -> None:
    path = _copy_client(tmp_path, clients_dir)
    _edit_yaml(path, lambda d: d.update(client_id="other"))
    with pytest.raises(ConfigError, match="client_id must be"):
        load_client_config("truechoice", tmp_path)


def test_missing_client_folder(clients_dir: Path) -> None:
    with pytest.raises(ConfigError, match="missing config file"):
        load_client_config("doesnotexist", clients_dir)


# --------------------------------------------------------------------------- secrets


def test_secret_name_mapping() -> None:
    assert secret_name_to_env_var("truechoice/whatsapp_token") == "TRUECHOICE_WHATSAPP_TOKEN"
    assert secret_name_to_env_var("deepseek_api_key") == "DEEPSEEK_API_KEY"


def test_env_secret_resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    resolver = EnvSecretResolver(read_dotenv_file=False)
    monkeypatch.setenv("TRUECHOICE_WHATSAPP_TOKEN", "abc")
    assert resolver.get("truechoice/whatsapp_token") == "abc"
    monkeypatch.delenv("TRUECHOICE_WHATSAPP_TOKEN")
    with pytest.raises(SecretNotFound, match="TRUECHOICE_WHATSAPP_TOKEN"):
        resolver.get("truechoice/whatsapp_token")
