"""The paper Gantt is a real dependency- + GPU-constrained schedule."""
import pytest

from backend.app import paper_gantt as G


@pytest.fixture
def client(arui_env, fake_subprocess):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from backend.app.api import router
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as c:
        yield c


def _task(tasks, tid):
    return next(t for t in tasks if t["id"] == tid)


def test_independent_tasks_run_in_parallel_on_enough_gpus():
    s = G.schedule([{"id": "a", "est_time_sec": 100},
                    {"id": "b", "est_time_sec": 100}], n_gpus=2)
    a, b = _task(s["tasks"], "a"), _task(s["tasks"], "b")
    assert a["start_sec"] == 0 and b["start_sec"] == 0     # parallel
    assert a["gpu"] != b["gpu"]
    assert s["makespan_sec"] == 100


def test_tasks_serialize_on_one_gpu():
    s = G.schedule([{"id": "a", "est_time_sec": 100},
                    {"id": "b", "est_time_sec": 100}], n_gpus=1)
    assert s["makespan_sec"] == 200                        # serial
    starts = sorted(t["start_sec"] for t in s["tasks"])
    assert starts == [0, 100]


def test_dependency_is_respected():
    s = G.schedule([{"id": "a", "est_time_sec": 100},
                    {"id": "b", "est_time_sec": 50, "depends_on": ["a"]}],
                   n_gpus=4)
    a, b = _task(s["tasks"], "a"), _task(s["tasks"], "b")
    assert b["start_sec"] >= a["end_sec"]                  # b waits for a
    assert s["critical_path"] == ["a", "b"]


def test_multi_gpu_task_blocks_multiple_gpus():
    s = G.schedule([{"id": "big", "est_time_sec": 100, "gpus_required": 2},
                    {"id": "x", "est_time_sec": 100},
                    {"id": "y", "est_time_sec": 100}], n_gpus=2)
    big = _task(s["tasks"], "big")
    assert len(big["gpus"]) == 2
    # x and y cannot both run while big holds both GPUs -> makespan 200
    assert s["makespan_sec"] == 200


def test_total_gpu_sec_accounts_for_width():
    s = G.schedule([{"id": "big", "est_time_sec": 100, "gpus_required": 2}],
                   n_gpus=2)
    assert s["total_gpu_sec"] == 200                       # 100s x 2 GPUs


def test_empty_schedule_is_safe():
    s = G.schedule([], n_gpus=3)
    assert s["tasks"] == [] and s["makespan_sec"] == 0 and s["critical_path"] == []


def test_gantt_endpoint_schedules_paper_runs(client, make_project, make_run):
    make_project()
    make_run(id="pr-a", run_name="a", status="queued", context="paper",
             est_time_sec=100, gpus_required=1, depends_on=[])
    make_run(id="pr-b", run_name="b", status="queued", context="paper",
             est_time_sec=50, gpus_required=1, depends_on=["pr-a"])
    # a non-paper run must be ignored by the paper Gantt
    make_run(id="r-x", run_name="x", status="running", est_time_sec=999)
    d = client.get("/api/paper/gantt").json()
    ids = {t["id"] for t in d["tasks"]}
    assert ids == {"pr-a", "pr-b"}
    assert d["critical_path"] == ["pr-a", "pr-b"]   # b waits on a
    assert d["n_gpus"] == 1                          # no GPUs registered in test
