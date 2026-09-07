"""The PI/supervisor keeps PAPER mode unblocked too."""
from backend.app import supervisor as S


def test_restart_dead_author_in_working_phase(arui_env):
    action, reason = S._paper_action("paper.draft_v0", False, False, 0)
    assert action == "restart" and "draft_v0" in reason


def test_keeps_recovering_after_three_restarts(arui_env):
    action, _ = S._paper_action("paper.run_ablations", False, False, 3)
    assert action == "restart"


def test_noop_when_author_alive(arui_env):
    assert S._paper_action("paper.draft_v0", False, True, 0)[0] is None


def test_noop_when_waiting_on_operator(arui_env):
    # operator_review WAITS for the human; a dead author there is not a stall
    assert S._paper_action("paper.operator_review", False, False, 0)[0] is None


def test_noop_when_submission_ready(arui_env):
    assert S._paper_action("paper.submission_ready", False, False, 0)[0] is None


def test_noop_when_paper_mode_inactive(arui_env):
    # fallback_used == paper mode hasn't actually started
    assert S._paper_action("paper.draft_v0", True, False, 0)[0] is None


def test_supervise_paper_mode_is_safe_when_no_paper(arui_env, fake_subprocess):
    # no paper phase set + tmux stubbed -> must not raise / not act
    S._supervise_paper_mode()
    S.tick()


# ── boot-parking re-feed (author alive but never started) ──────────────────

def test_refeed_when_parked_at_boot(arui_env):
    # alive, idle pane, no phase reported, past the boot grace -> re-feed
    assert S._should_refeed(fallback_used=True, alive=True, busy=False,
                            spawn_age=300, feed_remediations=0) is True


def test_no_refeed_while_still_booting(arui_env):
    assert S._should_refeed(True, True, False, 30, 0) is False


def test_no_refeed_once_phase_reported(arui_env):
    assert S._should_refeed(False, True, False, 999, 0) is False


def test_no_refeed_when_pane_busy(arui_env):
    assert S._should_refeed(True, True, True, 999, 0) is False


def test_no_refeed_when_author_dead(arui_env):
    assert S._should_refeed(True, False, False, 999, 0) is False


def test_refeed_does_not_permanently_give_up(arui_env):
    assert S._should_refeed(True, True, False, 999, 30) is True


def test_author_restart_has_capped_backoff(arui_env):
    assert S._author_restart_due(14, 0) is False
    assert S._author_restart_due(15, 0) is True
    assert S._author_restart_due(299, 30) is False
    assert S._author_restart_due(300, 30) is True
