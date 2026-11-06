"""Страж настроек: образец .env не расходится с кодом, адрес нейросети задаётся явно."""
import re
from pathlib import Path

import pytest
from conftest import run

import core
import main

ROOT = Path(__file__).resolve().parents[1]
INTERNAL = {"REQ_STAMP"}


def _vars_read_by_code() -> set[str]:
    found = set()
    for path in [*ROOT.glob("*.py"), *ROOT.glob("services/*.py")]:
        src = path.read_text(encoding="utf-8")
        found |= set(re.findall(r'(?:getenv|environ\.get|need|ids|env|number)\(\s*["\']([A-Z][A-Z0-9_]+)["\']', src))
        found |= set(re.findall(r'environ\[["\']([A-Z][A-Z0-9_]+)["\']\]', src))
        found |= set(re.findall(r'_env\(\s*["\']([A-Z][A-Z0-9_]+)["\']', src))
    sh = (ROOT / "entrypoint.sh").read_text(encoding="utf-8")
    found |= set(re.findall(r"\$\{([A-Z][A-Z0-9_]+)", sh))
    return found - INTERNAL  # внутренние переменные для тестов в образец не нужны


def test_every_setting_the_code_reads_is_listed_in_env_example():
    example = (ROOT / "env.example").read_text(encoding="utf-8")
    listed = set(re.findall(r"^#? ?([A-Z][A-Z0-9_]+)=", example, flags=re.M))
    missing = _vars_read_by_code() - listed
    assert not missing, f"В env.example нет настроек, которые читает код: {sorted(missing)}"


def test_example_does_not_advertise_settings_the_code_ignores():
    example = (ROOT / "env.example").read_text(encoding="utf-8")
    listed = set(re.findall(r"^#? ?([A-Z][A-Z0-9_]+)=", example, flags=re.M))
    assert not (listed - _vars_read_by_code()), "в образце есть настройки, которых код не читает (например LLM_MODEL)"


def test_no_silent_openai_default():
    source = "\n".join(path.read_text() for path in [ROOT / "core.py", *ROOT.glob("services/*.py")])
    assert "api.openai.com" not in source


def test_missing_base_url_is_reported(monkeypatch):
    monkeypatch.setattr(core, "PROVIDER", "openai")
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    assert "LLM_BASE_URL" in core.llm_config_problem()
    with pytest.raises(RuntimeError, match="LLM_BASE_URL"):
        run(core._openai("sys", "text", None))  # до запроса в сеть не доходит


def test_aitunnel_key_with_foreign_address_is_reported(monkeypatch):
    monkeypatch.setattr(core, "PROVIDER", "openai")
    monkeypatch.setenv("LLM_API_KEY", "sk-aitunnel-abc")
    monkeypatch.setenv("LLM_BASE_URL", "https://api.openai.com/v1")
    assert "AITunnel" in core.llm_config_problem()
    monkeypatch.setenv("LLM_BASE_URL", "https://api.aitunnel.ru/v1")
    assert core.llm_config_problem() is None


def test_anthropic_provider_does_not_need_base_url(monkeypatch):
    monkeypatch.setattr(core, "PROVIDER", "anthropic")
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    assert core.llm_config_problem() is None


@pytest.mark.parametrize("value", ["0", "3", "invalid"])
def test_material_worker_setting_is_validated_before_start(monkeypatch, value):
    monkeypatch.setenv("MATERIAL_WORKERS", value)
    assert "MATERIAL_WORKERS" in core.ops.config_problem()


def test_bot_refuses_to_start_without_base_url(monkeypatch):
    monkeypatch.setattr(core, "PROVIDER", "openai")
    monkeypatch.setenv("TG_TOKEN", "123456:ABCDEF")
    monkeypatch.setenv("TG_CHANNEL", "-1")
    monkeypatch.setenv("TG_APPROVERS", "1")
    monkeypatch.setenv("LLM_API_KEY", "k")
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    with pytest.raises(SystemExit, match="LLM_BASE_URL"):
        run(main.main())
