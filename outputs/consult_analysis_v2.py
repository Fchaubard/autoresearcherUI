"""Consult Gemini 2.5 Pro and GPT-5 high on docs/12-analysis-v2-spec.md.

Reads the spec, sends it to both models with a strong critique prompt,
and writes their full responses to docs/external-review-analysis-v2-*.md.

Uses the same .deploy/keys.env we already source.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Load keys
keys_env = ROOT / ".deploy" / "keys.env"
if keys_env.exists():
    for ln in keys_env.read_text().splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#") or "=" not in ln:
            continue
        k, v = ln.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

SPEC = (ROOT / "docs" / "12-analysis-v2-spec.md").read_text()

REVIEW_PROMPT = """You are a senior engineer who has shipped multiple
production ML dashboards (something like Weights & Biases). Read the spec
below and give an honest, opinionated review. Focus on:

1. PERFORMANCE TRAPS — anything that will be slow at 1000+ runs / multi-
   million-step series, that the spec misses.
2. UX ISSUES — anything in the proposed UX that is worse than W&B's,
   anything that will frustrate the user once they actually live in it.
3. ARCHITECTURAL GAPS — anything the spec leaves undefined that will
   become a refactor later.
4. SIMPLER ALTERNATIVES — anywhere the spec is over-engineered and a
   simpler design would deliver the same value.
5. CONCRETE FIXES — for each issue you raise, propose the specific change
   you'd make to the spec.

Be direct. The user is a sharp engineer who wants real feedback, not
hedging. Recommend dropping any features you think are mistakes.

Format your reply as Markdown with section headers for each of the five
categories above. Skip categories where you have nothing to say. End
with a TL;DR of the top 3 changes you'd make.

=== SPEC v1 ===

""" + SPEC


def call_gemini():
    key = os.environ["GEMINI_API_KEY"]
    url = ("https://generativelanguage.googleapis.com/v1beta/"
           "models/gemini-2.5-pro:generateContent?key=" + key)
    body = {
        "contents": [{"role": "user",
                      "parts": [{"text": REVIEW_PROMPT}]}],
        "generationConfig": {"temperature": 0.4},
    }
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as resp:
        data = json.loads(resp.read())
    return data["candidates"][0]["content"]["parts"][0]["text"]


def call_openai():
    key = os.environ["OPENAI_API_KEY"]
    body = {
        "model": "gpt-5",
        "messages": [{"role": "user", "content": REVIEW_PROMPT}],
        "reasoning_effort": "high",
    }
    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Authorization": "Bearer " + key,
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as resp:
        data = json.loads(resp.read())
    return data["choices"][0]["message"]["content"]


print(">>> Calling Gemini 2.5 Pro ...", flush=True)
try:
    g = call_gemini()
    (ROOT / "docs" / "external-review-analysis-v2-gemini.md").write_text(
        "# Analysis v2 review — Gemini 2.5 Pro\n\n" + g)
    print(f"    Gemini wrote {len(g)} chars", flush=True)
except Exception as e:
    print("    Gemini FAILED:", e, flush=True)

print(">>> Calling GPT-5 high ...", flush=True)
try:
    o = call_openai()
    (ROOT / "docs" / "external-review-analysis-v2-openai.md").write_text(
        "# Analysis v2 review — GPT-5 high\n\n" + o)
    print(f"    GPT-5 wrote {len(o)} chars", flush=True)
except Exception as e:
    print("    GPT-5 FAILED:", e, flush=True)

print(">>> Done.", flush=True)
