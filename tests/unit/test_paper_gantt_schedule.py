"""The Gantt scheduler must pin RUNNING runs to now (they are already on a GPU),
so the N running runs overlap at the current time instead of being bin-packed
into future slots. Queued/proposed runs schedule after a GPU frees.
"""
from backend.app import paper_gantt as g


def test_running_runs_pinned_to_now_and_overlap():
    runs = [
        {"id": "r1", "status": "running", "est_time_sec": 3000},
        {"id": "r2", "status": "running", "est_time_sec": 3000},
        {"id": "r3", "status": "running", "est_time_sec": 3000},
        {"id": "q1", "status": "queued", "est_time_sec": 3000},
    ]
    out = g.schedule(runs, n_gpus=3)
    placed = {t["id"]: t for t in out["tasks"]}
    # all three running runs start NOW (overlap)
    assert placed["r1"]["start_sec"] == 0
    assert placed["r2"]["start_sec"] == 0
    assert placed["r3"]["start_sec"] == 0
    # each running run occupies a distinct GPU
    assert len({placed[r]["gpu"] for r in ("r1", "r2", "r3")}) == 3
    # the queued run waits until a GPU frees up
    assert placed["q1"]["start_sec"] >= 3000


def test_running_uses_remaining_time_not_full():
    out = g.schedule([{"id": "r1", "status": "running",
                       "est_time_sec": 3000, "remaining_sec": 600}], n_gpus=3)
    t = out["tasks"][0]
    assert t["start_sec"] == 0 and t["est_time_sec"] == 600


def test_overrun_running_gets_visible_bar_not_sliver():
    # remaining_sec <= 0 (run blew past its estimate): the bar must NOT collapse
    # to ~0; it gets a 5-min "winding down" bar so the UI can show + flag it.
    out = g.schedule([{"id": "r1", "status": "running",
                       "est_time_sec": 12000, "remaining_sec": -37000}],
                     n_gpus=3)
    t = out["tasks"][0]
    assert t["start_sec"] == 0
    assert t["end_sec"] >= 300        # visible bar, not a 0/60s sliver


def test_more_running_than_gpus_still_starts_at_now():
    # 4 running on 3 GPUs: all still pinned to now (over-subscribed is honest)
    runs = [{"id": f"r{i}", "status": "running", "est_time_sec": 1000}
            for i in range(4)]
    out = g.schedule(runs, n_gpus=3)
    assert all(t["start_sec"] == 0 for t in out["tasks"])


def test_queued_only_still_schedules():
    runs = [{"id": f"q{i}", "status": "queued", "est_time_sec": 1000}
            for i in range(6)]
    out = g.schedule(runs, n_gpus=3)
    starts = sorted(t["start_sec"] for t in out["tasks"])
    # first wave at 0, second wave at 1000
    assert starts[:3] == [0, 0, 0] and starts[3:] == [1000, 1000, 1000]
