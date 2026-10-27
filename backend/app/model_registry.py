"""Canonical LLM model/capability registry used by API, UI and workers."""
from __future__ import annotations

OPENAI_EFFORTS = ("none", "low", "medium", "high", "xhigh", "max")
GEMINI_EFFORTS = ("low", "medium", "high")

# Models exposed to autonomous coding-agent roles. API-only compatibility
# models remain available to council/scoping/PI roles. Updating future CLI
# availability is intentionally a single-set edit here.
AGENT_CLI_MODELS = {
    "gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna",
    "gpt-5.5", "gpt-5.2",
    "claude-fable-5-1", "claude-opus-5", "claude-fable-5",
    "claude-sonnet-5", "claude-haiku-4-5-20251001", "claude-opus-4-8",
    "gemini-4-argon", "gemini-3.1-flash-lite",
    "gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash",
    "gemini-3.5-flash", "gemini-3.5-flash-lite", "gemini-3.1-pro-preview",
    "gemini-2.5-pro", "gemini-2.5-flash",
}

# Official OpenCode Console chat catalog, verified 2026-10-27. Jev is a
# decision endpoint, not a chat/tool model, and cannot fill research roles.
OPENCODE_FREE_MODELS = (
    ("nemotron-3-ultra-free", "Nemotron 3 Ultra · free"),
    ("nemotron-3.5-lightning-free", "Nemotron 3.5 Lightning · free"),
    ("nemotron-3.5-super-vl-free", "Nemotron 3.5 Super VL · free"),
    ("big-pickle", "Big Pickle · free"),
    ("glm-5.3-flash-free", "GLM 5.3 Flash · free"),
    ("deepseek-v4.1-flash-free", "DeepSeek V4.1 Flash · free"),
    ("beam-free", "Beam · free"),
    ("flint-40b-free", "Flint 40B · free hosted"),
    ("mimo-v2.6-flash-free", "MiMo V2.6 Flash · free"),
)
AGENT_CLI_MODELS.update("opencode/" + model for model, _ in OPENCODE_FREE_MODELS)
AGENT_CLI_MODELS.add("flint/local")

MODELS = (
    # OpenAI API models (current family first, compatibility models retained).
    {"id": "gpt-6-astra", "label": "GPT-6 Astra", "provider": "openai", "efforts": ("low", "medium", "high", "xhigh", "max")},
    {"id": "gpt-5.6-sol", "label": "GPT-5.6 Sol", "provider": "openai", "efforts": OPENAI_EFFORTS},
    {"id": "gpt-5.6-terra", "label": "GPT-5.6 Terra", "provider": "openai", "efforts": OPENAI_EFFORTS},
    {"id": "gpt-5.6-luna", "label": "GPT-5.6 Luna", "provider": "openai", "efforts": OPENAI_EFFORTS},
    {"id": "gpt-5.5", "label": "GPT-5.5", "provider": "openai", "efforts": ("low", "medium", "high", "xhigh")},
    {"id": "gpt-5.2", "label": "GPT-5.2", "provider": "openai", "efforts": ("low", "medium", "high", "xhigh")},
    {"id": "gpt-5.1", "label": "GPT-5.1", "provider": "openai", "efforts": ("low", "medium", "high")},
    {"id": "gpt-5", "label": "GPT-5", "provider": "openai", "efforts": ("minimal", "low", "medium", "high")},
    {"id": "gpt-5-mini", "label": "GPT-5 Mini", "provider": "openai", "efforts": ("minimal", "low", "medium", "high")},
    {"id": "gpt-5-nano", "label": "GPT-5 Nano", "provider": "openai", "efforts": ("minimal", "low", "medium", "high")},
    {"id": "o3", "label": "o3", "provider": "openai", "efforts": ("low", "medium", "high")},
    {"id": "o3-pro", "label": "o3 Pro", "provider": "openai", "efforts": ("low", "medium", "high")},
    # Gemini text/agent models.
    {"id": "gemini-4-argon", "label": "Gemini 4 Argon", "provider": "gemini", "efforts": GEMINI_EFFORTS},
    {"id": "gemini-3.1-flash-lite", "label": "Gemini 3.1 Flash-Lite", "provider": "gemini", "efforts": GEMINI_EFFORTS},
    {"id": "gemini-3.8-flash", "label": "Gemini 3.8 Flash", "provider": "gemini", "efforts": GEMINI_EFFORTS},
    {"id": "gemini-3.7-flash", "label": "Gemini 3.7 Flash", "provider": "gemini", "efforts": GEMINI_EFFORTS},
    {"id": "gemini-3.6-flash", "label": "Gemini 3.6 Flash", "provider": "gemini", "efforts": GEMINI_EFFORTS},
    {"id": "gemini-3.5-flash", "label": "Gemini 3.5 Flash", "provider": "gemini", "efforts": GEMINI_EFFORTS},
    {"id": "gemini-3.5-flash-lite", "label": "Gemini 3.5 Flash-Lite", "provider": "gemini", "efforts": GEMINI_EFFORTS},
    {"id": "gemini-3.1-pro-preview", "label": "Gemini 3.1 Pro Preview", "provider": "gemini", "efforts": GEMINI_EFFORTS},
    {"id": "gemini-2.5-pro", "label": "Gemini 2.5 Pro", "provider": "gemini", "efforts": ()},
    {"id": "gemini-2.5-flash", "label": "Gemini 2.5 Flash", "provider": "gemini", "efforts": ()},
    # Current Claude API names plus still-supported compatibility choices.
    {"id": "claude-fable-5-1", "label": "Claude Fable 5.1", "provider": "claude", "efforts": ()},
    {"id": "claude-opus-5", "label": "Claude Opus 5", "provider": "claude", "efforts": ()},
    {"id": "claude-sonnet-5", "label": "Claude Sonnet 5", "provider": "claude", "efforts": ()},
    {"id": "claude-fable-5", "label": "Claude Fable 5", "provider": "claude", "efforts": ()},
    {"id": "claude-opus-4-8", "label": "Claude Opus 4.8", "provider": "claude", "efforts": ()},
    {"id": "claude-opus-4-7", "label": "Claude Opus 4.7", "provider": "claude", "efforts": ()},
    {"id": "claude-opus-4-6", "label": "Claude Opus 4.6", "provider": "claude", "efforts": ()},
    {"id": "claude-sonnet-4-6", "label": "Claude Sonnet 4.6", "provider": "claude", "efforts": ()},
    {"id": "claude-haiku-4-5-20251001", "label": "Claude Haiku 4.5", "provider": "claude", "efforts": ()},
) + tuple({"id": "opencode/" + model, "label": label, "provider": "opencode",
           "efforts": (), "free": True, "transport": "opencode_cli"}
          for model, label in OPENCODE_FREE_MODELS) + (
    {"id": "flint/local", "label": "Flint · local server", "provider": "flint",
     "efforts": (), "local": True, "transport": "openai_compatible"},
)


def provider_for(model: str) -> str | None:
    model = (model or "").strip().lower()
    hit = next((m for m in MODELS if m["id"] == model), None)
    if hit:
        return str(hit["provider"])
    if model.startswith("opencode/"):
        return "opencode"
    if model in {"flint", "recursivesi/flint"} or model.startswith("flint/"):
        return "flint"
    if model.startswith("gemini-"):
        return "gemini"
    if model.startswith("claude-") or model in {"fable", "opus", "sonnet", "haiku"}:
        return "claude"
    if model.startswith(("gpt-", "o1", "o3", "o4")):
        return "openai"
    return None


def efforts_for(model: str) -> tuple[str, ...]:
    hit = next((m for m in MODELS if m["id"] == (model or "").strip().lower()), None)
    return tuple(hit.get("efforts") or ()) if hit else ()


def public_registry() -> dict:
    return {"models": [dict(m, efforts=list(m.get("efforts") or ()),
                            agent_cli=m["id"] in AGENT_CLI_MODELS)
                       for m in MODELS],
            "defaults": {"claude": "claude-opus-5", "openai": "gpt-6-astra",
                         "gemini": "gemini-3.8-flash",
                         "opencode": "opencode/nemotron-3-ultra-free",
                         "flint": "flint/local"}}
