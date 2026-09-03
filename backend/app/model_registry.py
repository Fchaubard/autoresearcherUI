"""Canonical LLM model/capability registry used by API, UI and workers."""
from __future__ import annotations

OPENAI_EFFORTS = ("none", "low", "medium", "high", "xhigh", "max")
GEMINI_EFFORTS = ("low", "medium", "high")

MODELS = (
    # OpenAI API models (current family first, compatibility models retained).
    {"id": "gpt-5.6-sol", "label": "GPT-5.6 Sol", "provider": "openai", "efforts": OPENAI_EFFORTS},
    {"id": "gpt-5.6-terra", "label": "GPT-5.6 Terra", "provider": "openai", "efforts": OPENAI_EFFORTS},
    {"id": "gpt-5.6-luna", "label": "GPT-5.6 Luna", "provider": "openai", "efforts": OPENAI_EFFORTS},
    {"id": "gpt-5.2", "label": "GPT-5.2", "provider": "openai", "efforts": ("low", "medium", "high", "xhigh")},
    {"id": "gpt-5.1", "label": "GPT-5.1", "provider": "openai", "efforts": ("low", "medium", "high")},
    {"id": "gpt-5", "label": "GPT-5", "provider": "openai", "efforts": ("minimal", "low", "medium", "high")},
    {"id": "gpt-5-mini", "label": "GPT-5 Mini", "provider": "openai", "efforts": ("minimal", "low", "medium", "high")},
    {"id": "gpt-5-nano", "label": "GPT-5 Nano", "provider": "openai", "efforts": ("minimal", "low", "medium", "high")},
    {"id": "o3", "label": "o3", "provider": "openai", "efforts": ("low", "medium", "high")},
    {"id": "o3-pro", "label": "o3 Pro", "provider": "openai", "efforts": ("low", "medium", "high")},
    # Gemini text/agent models.
    {"id": "gemini-3.8-flash", "label": "Gemini 3.8 Flash", "provider": "gemini", "efforts": GEMINI_EFFORTS},
    {"id": "gemini-3.7-flash", "label": "Gemini 3.7 Flash", "provider": "gemini", "efforts": GEMINI_EFFORTS},
    {"id": "gemini-3.6-flash", "label": "Gemini 3.6 Flash", "provider": "gemini", "efforts": GEMINI_EFFORTS},
    {"id": "gemini-3.5-flash", "label": "Gemini 3.5 Flash", "provider": "gemini", "efforts": GEMINI_EFFORTS},
    {"id": "gemini-3.5-flash-lite", "label": "Gemini 3.5 Flash-Lite", "provider": "gemini", "efforts": GEMINI_EFFORTS},
    {"id": "gemini-3.1-pro-preview", "label": "Gemini 3.1 Pro Preview", "provider": "gemini", "efforts": GEMINI_EFFORTS},
    {"id": "gemini-2.5-pro", "label": "Gemini 2.5 Pro", "provider": "gemini", "efforts": ()},
    {"id": "gemini-2.5-flash", "label": "Gemini 2.5 Flash", "provider": "gemini", "efforts": ()},
    # Current Claude API names plus still-supported compatibility choices.
    {"id": "claude-opus-5", "label": "Claude Opus 5", "provider": "claude", "efforts": ()},
    {"id": "claude-sonnet-5", "label": "Claude Sonnet 5", "provider": "claude", "efforts": ()},
    {"id": "claude-fable-5", "label": "Claude Fable 5", "provider": "claude", "efforts": ()},
    {"id": "claude-opus-4-8", "label": "Claude Opus 4.8", "provider": "claude", "efforts": ()},
    {"id": "claude-opus-4-7", "label": "Claude Opus 4.7", "provider": "claude", "efforts": ()},
    {"id": "claude-opus-4-6", "label": "Claude Opus 4.6", "provider": "claude", "efforts": ()},
    {"id": "claude-sonnet-4-6", "label": "Claude Sonnet 4.6", "provider": "claude", "efforts": ()},
    {"id": "claude-haiku-4-5-20251001", "label": "Claude Haiku 4.5", "provider": "claude", "efforts": ()},
)


def provider_for(model: str) -> str | None:
    model = (model or "").strip().lower()
    hit = next((m for m in MODELS if m["id"] == model), None)
    if hit:
        return str(hit["provider"])
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
    return {"models": [dict(m, efforts=list(m.get("efforts") or ())) for m in MODELS],
            "defaults": {"claude": "claude-opus-5", "openai": "gpt-5.6-sol",
                         "gemini": "gemini-3.8-flash"}}
