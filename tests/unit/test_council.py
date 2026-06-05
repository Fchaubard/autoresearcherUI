"""Unit tests for backend.app.council — pure helpers + mocked LLMs."""
from __future__ import annotations


def test_strip_fences_no_fence(arui_env):
    from backend.app.council import _strip_fences
    assert _strip_fences("hello world") == "hello world"


def test_strip_fences_plain_backticks(arui_env):
    from backend.app.council import _strip_fences
    out = _strip_fences("```json\n{\"a\":1}\n```")
    assert out.startswith("{") and out.endswith("}")


def test_strip_fences_no_lang_label(arui_env):
    from backend.app.council import _strip_fences
    out = _strip_fences("```\n{\"x\":2}\n```")
    assert "{\"x\":2}" in out


def test_safe_parse_clean_json(arui_env):
    from backend.app.council import _safe_parse
    out = _safe_parse('{"verdict":"keep","learning":"good"}')
    assert out["verdict"] == "keep"
    assert out["learning"] == "good"
    # defaults are populated
    assert out["rerank_pending"] == []
    assert out["veto"] == []


def test_safe_parse_extracts_embedded_object(arui_env):
    from backend.app.council import _safe_parse
    out = _safe_parse(
        "I think the answer is: {\"verdict\":\"discard\"} ok?")
    assert out["verdict"] == "discard"


def test_safe_parse_garbage_returns_none(arui_env):
    from backend.app.council import _safe_parse
    assert _safe_parse("not json at all") is None


def test_safe_parse_returns_none_for_non_dict(arui_env):
    from backend.app.council import _safe_parse
    assert _safe_parse("[1, 2, 3]") is None


def test_agreement_matching_verdicts(arui_env):
    from backend.app.council import _agreement
    a = {"verdict": "keep", "rerank_pending": ["i1", "i2", "i3"],
         "veto": ["bad"]}
    b = {"verdict": "keep", "rerank_pending": ["i3", "i1", "i2"],
         "veto": ["bad"]}
    assert _agreement(a, b) is True


def test_agreement_different_verdicts(arui_env):
    from backend.app.council import _agreement
    a = {"verdict": "keep", "rerank_pending": [], "veto": []}
    b = {"verdict": "discard", "rerank_pending": [], "veto": []}
    assert _agreement(a, b) is False


def test_agreement_different_vetos(arui_env):
    from backend.app.council import _agreement
    a = {"verdict": "keep", "rerank_pending": [], "veto": ["x"]}
    b = {"verdict": "keep", "rerank_pending": [], "veto": ["y"]}
    assert _agreement(a, b) is False


def test_agreement_different_top3(arui_env):
    from backend.app.council import _agreement
    a = {"verdict": "keep", "rerank_pending": ["i1", "i2", "i3"], "veto": []}
    b = {"verdict": "keep", "rerank_pending": ["i4", "i5", "i6"], "veto": []}
    assert _agreement(a, b) is False


def test_settings_returns_defaults(arui_env):
    from backend.app.council import _settings, DEFAULTS
    s = _settings()
    for k, v in DEFAULTS.items():
        assert s[k] == v


def test_settings_merges_user_overrides(arui_env, setting_setter):
    from backend.app.council import _settings
    setting_setter("onboarding", {"council_gemini_model": "gemini-1.5-flash",
                                    "run_debate": False})
    s = _settings()
    assert s["council_gemini_model"] == "gemini-1.5-flash"
    assert s["run_debate"] is False


def test_available_reviewers_empty_without_keys(arui_env, monkeypatch):
    from backend.app.council import _available_reviewers, _settings
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert _available_reviewers(_settings()) == []


def test_available_reviewers_with_keys(arui_env, monkeypatch):
    from backend.app.council import _available_reviewers, _settings
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    monkeypatch.setenv("OPENAI_API_KEY", "y")
    s = _available_reviewers(_settings())
    assert "gemini" in s
    assert "openai" in s


def test_is_enabled_false_without_keys(arui_env, monkeypatch):
    from backend.app.council import is_enabled
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert is_enabled() is False


def test_claude_available(arui_env, monkeypatch):
    from backend.app.council import _claude_available, _settings
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    assert _claude_available(_settings()) is True


def test_review_async_no_reviewers(arui_env, monkeypatch):
    """If no reviewer keys, review_async returns False without launching."""
    from backend.app import council
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert council.review_async("anything") is False


# ─────────────────────────────────────────────────────────────────────────
# Lessons.md quality gate — classify_lesson_quality()
#
# These tests pin the behaviour described in task #4 (Improve lessons.md
# content quality). Bad lessons (tool-mechanics tips, missing HYPOTHESIS
# marker, no run_id) MUST be flagged so they're dropped before being
# written to the project's running scientific memory.
# ─────────────────────────────────────────────────────────────────────────


# Good lessons — these follow the HYPOTHESIS / RESULT / WHY / GENERALIZABLE
# INSIGHT / NEXT EXPERIMENT contract and cite at least one run_id.
_GOOD_LESSONS = [
    (
        "HYPOTHESIS: 5-way diffusion-LM ensemble outperforms AR baseline "
        "on GSM8K val.\n"
        "RESULT: diff_n5_seed7 reached 0.0432 EM and diff_n5_seed11 "
        "reached 0.0441 EM vs ar_baseline_v2 at 0.0561 EM.\n"
        "WHY: ensemble members collapse to the same answer distribution "
        "after 2k steps; voting does not recover the variance the design "
        "assumed.\n"
        "GENERALIZABLE INSIGHT: in-distribution diff-init is dominated by "
        "AR-finetuned-init at <1B params.\n"
        "NEXT EXPERIMENT: diff_init_from_ar_ckpt with lr=1e-4, fp32, n=3."
    ),
    (
        "HYPOTHESIS: lowering lr from 5e-4 to 1e-4 prevents the divergence "
        "seen in earlier diffusion-LM runs.\n"
        "RESULT: diff_lr1e4_a, diff_lr1e4_b, diff_lr1e4_c all completed "
        "without NaNs and landed 0.038-0.042 EM; previous lr5e4 cohort "
        "diverged within 200 steps.\n"
        "WHY: the diffusion objective is sensitive to early-step scaling.\n"
        "GENERALIZABLE INSIGHT: for diffusion-LM finetuning at this scale, "
        "lr ≤ 1e-4 is the stability boundary.\n"
        "NEXT EXPERIMENT: lr=3e-5 sweep at the same architecture."
    ),
]


# Bad lessons — these are the actual failure patterns observed in the live
# lessons.md and called out in the RESEARCH_IMPROVEMENT_PLAN.
_BAD_LESSONS = [
    # 1. Pure tool-mechanics platitude (no HYPOTHESIS, no run_id)
    (
        "Always log all metrics via arui.summary so the dashboard can plot "
        "them. Remember to set __METRIC__ on every run.",
        "tool_mechanics",                       # any of these prefixes
    ),
    # 2. Process nag with no research content
    (
        "For the 32nd consecutive batch the agent ignored the council's "
        "recommendation to build trusted_eval. Need to escalate.",
        "tool_mechanics",
    ),
    # 3. Vague platitude, no HYPOTHESIS marker, no run_id
    (
        "More runs needed before we can draw conclusions. The data is "
        "inconclusive without further investigation.",
        "tool_mechanics",
    ),
    # 4. Has HYPOTHESIS but no run_id anywhere — DROP
    (
        "HYPOTHESIS: ensemble of diffusion LMs beats AR baseline.\n"
        "RESULT: the ensemble underperformed the baseline by a wide "
        "margin across multiple seeds.\n"
        "WHY: members collapse.\n"
        "GENERALIZABLE INSIGHT: ensembling does not help at this scale.\n"
        "NEXT EXPERIMENT: try a different init.",
        "no_run_id",
    ),
    # 5. Has run_ids but no HYPOTHESIS marker — DROP (free-form prose)
    (
        "diff_n5_seed7 hit 0.0432 EM and ar_baseline_v2 hit 0.0561 EM. "
        "We should think about what this means.",
        "no_hypothesis_marker",
    ),
    # 6. Empty
    ("", "empty"),
    # 7. Too short
    ("HYPOTHESIS: x", "too_short"),
]


def test_classify_lesson_quality_accepts_good_examples(arui_env):
    from backend.app.council import classify_lesson_quality
    for txt in _GOOD_LESSONS:
        q = classify_lesson_quality(txt)
        assert q["ok"] is True, (
            f"good lesson was flagged as bad: reason={q['reason']!r}\n{txt}")
        assert q["has_hypothesis"] is True
        assert q["has_run_id"] is True
        assert q["bad_phrase"] is None


def test_classify_lesson_quality_flags_bad_examples(arui_env):
    from backend.app.council import classify_lesson_quality
    for txt, expected_reason_prefix in _BAD_LESSONS:
        q = classify_lesson_quality(txt)
        assert q["ok"] is False, (
            f"bad lesson was accepted as good!\n{txt}")
        # Some bad reasons are like "tool_mechanics:always log" — match prefix.
        # "no_run_id" / "no_hypothesis_marker" / "empty" / "too_short" are
        # exact. We accept the loose "tool_mechanics" prefix bucket.
        if expected_reason_prefix == "tool_mechanics":
            assert q["reason"].startswith("tool_mechanics"), (
                f"expected a tool_mechanics flag, got {q['reason']!r}\n{txt}")
        else:
            assert q["reason"] == expected_reason_prefix, (
                f"expected reason={expected_reason_prefix!r}, "
                f"got {q['reason']!r}\n{txt}")


def test_classify_lesson_quality_flags_remember_to(arui_env):
    """Spot-check: 'remember to seed' style nags must be flagged even if
    they happen to contain a HYPOTHESIS marker and a run_id, because the
    banned-phrase check runs first."""
    from backend.app.council import classify_lesson_quality
    txt = ("HYPOTHESIS: seed_runs match.\n"
           "RESULT: run_abc123 and run_def456 agreed.\n"
           "Remember to seed every run consistently next time.")
    q = classify_lesson_quality(txt)
    assert q["ok"] is False
    assert q["reason"].startswith("tool_mechanics")


def test_append_lesson_drops_bad_quality(arui_env, setting_setter, tmp_path):
    """End-to-end: _append_lesson should NOT write a low-quality lesson to
    lessons.md even when the file path is valid. The classifier gate fires
    before the dedup gate."""
    from backend.app import council
    # _lessons_path needs an onboarding repo_name set
    setting_setter("onboarding", {"repo_name": "test-repo"})
    council._append_lesson(
        reviewer="openai (strategic)",
        run_name="batch of 5 runs",
        learning=("Always log all metrics via arui.summary and remember "
                  "to set __METRIC__ on every run."),
    )
    p = council._lessons_path()
    # File either doesn't exist or is empty — no lesson written.
    assert p is not None
    assert (not p.exists()) or p.read_text().strip() == ""


def test_append_lesson_keeps_good_quality(arui_env, setting_setter):
    """The complement: a well-formed lesson per the contract IS written."""
    from backend.app import council
    setting_setter("onboarding", {"repo_name": "test-repo"})
    learning = _GOOD_LESSONS[0]
    council._append_lesson(
        reviewer="claude (tiebreaker)",
        run_name="diff_n5_seed7",
        learning=learning,
    )
    p = council._lessons_path()
    assert p is not None and p.exists()
    body = p.read_text()
    assert "HYPOTHESIS:" in body
    assert "diff_n5_seed7" in body
    assert "claude (tiebreaker)" in body


def test_scan_lessons_file_counts_good_and_bad(arui_env, setting_setter):
    """The auditor function reads an existing lessons.md and reports stats
    we can show in the UI / digest."""
    from backend.app import council
    setting_setter("onboarding", {"repo_name": "test-repo"})
    p = council._lessons_path()
    assert p is not None
    # Hand-write a mixed file: 1 good entry, 2 bad entries.
    p.write_text(
        "- [2026-06-04 12:00 · openai on diff_n5_seed7] "
        + _GOOD_LESSONS[0].replace("\n", " ") + "\n"
        "- [2026-06-04 12:01 · openai on batch of 5 runs] "
        "Always log all metrics. Remember to set __METRIC__.\n"
        "- [2026-06-04 12:02 · gemini on batch of 5 runs] "
        "More runs needed; data is inconclusive.\n"
    )
    report = council.scan_lessons_file()
    assert report["total"] == 3
    assert report["ok"] == 1
    assert report["bad"] == 2
    # At least one of the bad ones should be a tool_mechanics reason.
    assert any(k.startswith("tool_mechanics")
               for k in report["bad_reasons"].keys())
    # samples_bad is capped at 5 and contains the raw lines so we can show
    # them in the Lessons UI as "rejected" entries.
    assert 1 <= len(report["samples_bad"]) <= 5
