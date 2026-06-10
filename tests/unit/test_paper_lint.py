"""The paper prose linter: bans em-dashes + AI-slop, without flagging
legitimate scientific prose or numeric ranges.
"""
from backend.app import paper_lint as L


def _kinds(text):
    return {v["kind"] for v in L.lint_prose(text)}


def test_flags_unicode_em_dash():
    v = L.lint_prose("The defense — surprisingly — fails.")
    assert any(x["kind"] == "emdash" for x in v)


def test_flags_latex_and_spaced_em_dash():
    assert _kinds("We show this---clearly---here.") == {"emdash"}
    assert _kinds("We show this -- clearly -- here.") == {"emdash"}


def test_flags_en_dash_between_words():
    assert "emdash" in _kinds("a state–of–the–art trick")


def test_numeric_ranges_are_not_flagged():
    # LaTeX en-dash range and unicode en-dash between numbers are legit
    assert L.lint_prose("Tables 1--3 and ASR 0.5–0.6 across seeds.") == []


def test_legit_scientific_negation_not_flagged():
    # NOT the AI antithesis — this is normal reporting and must pass clean
    assert L.lint_prose("ASR is not reduced, but remains high at 0.55.") == []


def test_flags_not_just_but_antithesis():
    assert "slop" in _kinds("This is not just a defense, but a new paradigm.")


def test_flags_its_not_x_its_y():
    assert "slop" in _kinds("It's not about detection, it's about removal.")


def test_flags_ai_tells():
    assert "slop" in _kinds("We delve into the rich tapestry of backdoors.")


def test_clean_prose_is_clean():
    txt = ("We plant a rare-string backdoor and measure attack success rate "
           "before and after our defense. The method reduces ASR to 0.0 on "
           "the held-out trigger while preserving clean accuracy.")
    assert L.lint_prose(txt) == []


def test_lint_paper_dir_and_format(tmp_path):
    (tmp_path / "intro.tex").write_text("A defense — the best — wins.\n")
    (tmp_path / "ok.tex").write_text("A plain clean sentence about ASR.\n")
    vs = L.lint_paper_dir(tmp_path)
    assert len(vs) == 1 and vs[0]["source"] == "intro.tex"
    assert "1 prose violation" in L.format_violations(vs)
    assert "clean" in L.format_violations([])
