"""Every advertised model survives each eligible role's save/read boundary."""
import pytest

from backend.app.model_registry import AGENT_CLI_MODELS, MODELS

ROLES = ("research_agent_model", "author_agent_model", "scoping_model",
         "council_gemini_model", "council_openai_model", "council_claude_model",
         "pi_agent_model")


@pytest.mark.parametrize("model", [m["id"] for m in MODELS])
def test_every_model_round_trips_in_every_eligible_role(model, arui_env, monkeypatch):
    from backend.app import api, agent_cli, provider_config, tmux_safe
    from backend.app.db import SessionLocal
    from backend.app.models import Setting
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    config = {"claude_token": "synthetic-claude", "openai_token": "synthetic-openai",
              "gemini_token": "synthetic-gemini", "flint_base_url": "http://127.0.0.1:30000/v1"}
    for name, value in provider_config.environment_from_config(config).items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("ARUI_CLAUDE_BIN", raising=False)
    monkeypatch.setattr(agent_cli, "_binary", lambda provider: "/bin/test-client")
    monkeypatch.setattr(provider_config, "opencode_binary", lambda: "/bin/test-client")
    monkeypatch.setattr(tmux_safe, "pane_alive", lambda role: False)
    with SessionLocal() as db:
        db.add(Setting(key="onboarding", value=config))
        db.commit()
    app = FastAPI()
    app.include_router(api.router)
    with TestClient(app) as client:
        for role in ROLES:
            if role in ROLES[:2] and model not in AGENT_CLI_MODELS:
                continue
            response = client.put("/api/settings", json={role: model})
            assert response.status_code == 200, (model, role, response.text)
            assert response.json()["saved"] is True
            assert client.get("/api/settings").json()[role] == model
            with SessionLocal() as db:
                saved = db.get(Setting, "onboarding")
                assert saved.value[role] == model
