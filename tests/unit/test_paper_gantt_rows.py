"""The Critical Path Gantt: /paper/gantt returns one row per run, grouped +
numbered per figure, with command + duration + status + scheduled start/end.
"""
import pytest


@pytest.fixture
def client(arui_env, fake_subprocess):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from backend.app.api import router
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as c:
        yield c


def test_figure_create(client, make_project):
    make_project()
    r = client.post("/api/paper/figures",
                    json={"title": "Figure 1: Val Acc vs Model Size",
                          "kind": "line"})
    assert r.status_code == 200
    j = r.json()
    assert j["ok"] is True and j["id"].startswith("pf-")


def test_figure_create_requires_title(client, make_project):
    make_project()
    assert client.post("/api/paper/figures", json={}).json()["ok"] is False


def test_gantt_rows_grouped_and_numbered_by_figure(client, make_project):
    make_project()
    f1 = client.post("/api/paper/figures",
                     json={"title": "Figure 1"}).json()["id"]
    f2 = client.post("/api/paper/figures",
                     json={"title": "Figure 2"}).json()["id"]
    # two runs for the first figure, one for the second
    for i in range(2):
        client.post("/api/paper/runs/queue", json={
            "name": f"f1_{i}", "figure_id": f1,
            "cmd": f"cd /w && python train.py --lr 1e-3 --seed {i}",
            "est_time_sec": 75600, "gpus_required": 1})
    client.post("/api/paper/runs/queue", json={
        "name": "f2_0", "figure_id": f2,
        "cmd": "python train.py --model_size 70m", "est_time_sec": 3600})

    d = client.get("/api/paper/gantt").json()
    tasks = d["tasks"]
    assert len(tasks) == 3
    # every row has the columns the table needs
    for k in ("figure_label", "run_number", "command", "duration_sec",
              "status", "start_iso", "end_iso", "start_sec", "end_sec"):
        assert k in tasks[0], k
    # grouped into exactly two figures
    by_label = {}
    for t in tasks:
        by_label.setdefault(t["figure_label"], []).append(t["run_number"])
    assert set(by_label) == {"Figure 1", "Figure 2"}
    # the figure with two runs is numbered 1..2 per figure
    two = [lbl for lbl, nums in by_label.items() if len(nums) == 2][0]
    assert sorted(by_label[two]) == [1, 2]
    # command + duration carried through
    assert any("train.py" in (t["command"] or "") for t in tasks)
    assert any(t["duration_sec"] == 75600 for t in tasks)


def test_enumerate_expands_the_grid(client, make_project):
    make_project()
    f1 = client.post("/api/paper/figures",
                     json={"title": "Figure 1"}).json()["id"]
    r = client.post("/api/paper/runs/enumerate", json={
        "figure_id": f1, "name_prefix": "f1",
        "arg_template": "--model {model} --lr {lr} --seed {seed}",
        "axes": {"model": ["m70", "m160"], "lr": [1e-4, 3e-4],
                 "seed": [0, 1, 2]},
        "est_time_sec": 75600})
    j = r.json()
    assert j["ok"] is True and j["n"] == 2 * 2 * 3       # full cartesian grid
    d = client.get("/api/paper/gantt").json()
    assert len(d["tasks"]) == 12
    assert all(t["figure_label"] == "Figure 1" for t in d["tasks"])
    assert any("--model m70" in (t["command"] or "") for t in d["tasks"])
    assert all(t["duration_sec"] == 75600 for t in d["tasks"])


def test_enumerate_requires_figure_and_axes(client, make_project):
    make_project()
    assert client.post("/api/paper/runs/enumerate",
                       json={"arg_template": "x", "axes": {"a": [1]}}
                       ).json()["ok"] is False
    f = client.post("/api/paper/figures", json={"title": "F"}).json()["id"]
    assert client.post("/api/paper/runs/enumerate",
                       json={"figure_id": f}).json()["ok"] is False


def test_enumerate_proposed_status_for_dry_run(client, make_project):
    make_project()
    f = client.post("/api/paper/figures", json={"title": "F"}).json()["id"]
    r = client.post("/api/paper/runs/enumerate", json={
        "figure_id": f, "arg_template": "--seed {seed}",
        "axes": {"seed": [0, 1]}, "status": "proposed"})
    assert r.json()["status"] == "proposed"
