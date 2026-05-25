"""Demo-data seed (doc 11 M0): loads a realistic, populated project so the
dashboard is beautiful and demoable before the research engine is built."""
from __future__ import annotations

import datetime as dt
import math
import random
import time

from . import metrics
from .db import SessionLocal
from .models import ChatMessage, Event, Gpu, Idea, JournalEntry, Project, Run

GPU_COUNT = 4
PROJECT_ID = "proj-bs1learning"
_rng = random.Random(7)


def _iso(days_ago: float = 0.0) -> str:
    return (dt.datetime.now(dt.timezone.utc)
            - dt.timedelta(days=days_ago)).isoformat()


def _curve(run_id: str, n: int, acc_final: float, loss_final: float) -> None:
    """Generate a train_loss (down) + val at5_acc (up) curve for a run."""
    points = []
    t0 = time.time() - n * 12
    for s in range(n):
        frac = s / max(1, n - 1)
        loss = (2.6 - (2.6 - loss_final) * (1 - math.exp(-3.2 * frac))
                + _rng.gauss(0, 0.06))
        acc = (0.30 + (acc_final - 0.30) * (1 - math.exp(-2.8 * frac))
               + _rng.gauss(0, 0.012))
        wt = t0 + s * 12
        points.append({"key": "train_loss", "step": s,
                       "value": max(0.05, loss), "wall_time": wt})
        points.append({"key": "at5_acc", "step": s,
                       "value": min(0.99, max(0.0, acc)), "wall_time": wt})
    metrics.append(run_id, points)


# (idea_id, description, why, ev, status, source, acc, delta, vram, gpu)
_IDEAS = [
    ("baseline", "Unmodified train.py: naive batch-size-1 SGD fine-tuning on "
     "LFW with a frozen ImageNet backbone.",
     "Establishes the reference number every later run is compared against.",
     0.0, "success", "seed", 0.412, 0.0, 18240, 0),
    ("icl-episodic-memory", "In-context learning: store each new example in a "
     "key-value episodic memory and retrieve top-k at inference. No weight "
     "update.",
     "Decouples plasticity from interference - one example has large local "
     "effect, zero catastrophic forgetting.",
     0.21, "success", "seed", 0.478, 0.066, 21110, 1),
    ("jepa-frozen-adapter", "Frozen JEPA backbone + a tiny per-identity "
     "adapter head, with a router selecting the head.",
     "Single example only shapes a tiny parameter subset; backbone knowledge "
     "is fully preserved.",
     0.34, "success", "seed", 0.531, 0.119, 23980, 2),
    ("grad-agreement-filter", "EMA gradient-agreement filtering: drop the "
     "single-example gradient when cosine sim to the EMA is below threshold.",
     "Filters noisy/destructive update directions before they touch weights.",
     0.18, "failed", "seed", 0.392, -0.020, 19400, 3),
    ("whitened-rolling-mixup", "Whitened rolling mix-up: train on a size-2 "
     "batch of (x_t,y_t) and the whitened EMA prototype (x_bar,y_bar).",
     "A virtual mini-batch damps gradient noise while injecting prior "
     "knowledge - pseudo-rehearsal without storing raw data.",
     0.12, "unclear", "seed", 0.418, 0.006, 18960, 0),
    ("proto-metric-insertion", "Prototype / metric-memory insertion: one image "
     "creates a class prototype + uncertainty ellipsoid in a whitened "
     "embedding space.",
     "If the representation is good, one example should instantiate a Voronoi "
     "cell, not distort the feature extractor.",
     0.39, "running", "agent", 0.0, 0.0, 22600, 0),
    ("dual-fast-slow-memory", "CLS-style fast episodic buffer + slow "
     "consolidation: write instantly to memory, replay-consolidate later.",
     "Two-timescale learning is the biological blueprint for one-shot + "
     "lifelong learning.",
     0.36, "running", "agent", 0.0, 0.0, 24300, 1),
    ("hypernet-lora-adapter", "Hypernetwork-generated rank-1 LoRA adapter, "
     "conditioned on the single example's embedding.",
     "Meta-learns to produce a good adapter from one shot; localizes the "
     "update to a disposable low-rank subspace.",
     0.31, "running", "agent", 0.0, 0.0, 23100, 2),
    ("trust-region-gated-update", "Trust-region update: project the new "
     "gradient onto the safe subspace (Fisher-weighted + orthogonal to "
     "protected directions).",
     "Turns 'drop it on the floor' into a principled interference-free "
     "update.",
     0.27, "running", "agent", 0.0, 0.0, 21800, 3),
    ("meta-selective-plasticity", "Meta-learn the network so one-example "
     "updates hit only sparse, context-gated plastic units (OML/ANML style).",
     "Closest to how humans actually do safe one-shot adaptation.",
     0.44, "not_implemented", "agent", 0.0, 0.0, 0, -1),
    ("knn-lm-datastore", "Non-parametric kNN-LM-style datastore over latent "
     "states, interpolated with the parametric prior.",
     "Proven to improve a frozen model with zero retraining.",
     0.29, "not_implemented", "agent", 0.0, 0.0, 0, -1),
    ("latent-generative-replay", "Latent generative replay: a small frozen "
     "decoder hallucinates 64 pseudo-samples to anchor each update.",
     "The new example sets direction; pseudo-samples provide friction against "
     "drift.",
     0.22, "not_implemented", "agent", 0.0, 0.0, 0, -1),
]


def seed_all() -> None:
    db = SessionLocal()
    if db.query(Project).first():
        db.close()
        return

    db.add(Project(
        id=PROJECT_ID, name="bs1learning",
        repo_url="https://github.com/Fchaubard/bs1learning",
        purpose="Uncover a new paradigm for meaningful learning from a single "
                "example (effective batch size = 1) without catastrophic "
                "forgetting - getting ML closer to how humans learn.",
        validation_metric="at5_acc", metric_direction="maximize",
        time_budget_sec=3600, status="running",
        baseline_run_id="baseline", gpu_count=GPU_COUNT,
        created_at=_iso(6)))

    for i in range(GPU_COUNT):
        db.add(Gpu(index=i, model="NVIDIA A40", total_vram_mb=49140))

    for n, (iid, desc, why, ev, status, src, acc, delta, vram, gpu) \
            in enumerate(_IDEAS):
        is_run = status in ("success", "failed", "unclear", "running")
        is_baseline = iid == "baseline"
        age = 6 - n * 0.45
        db.add(Idea(
            id=f"idea-{iid}", project_id=PROJECT_ID, idea_id=iid,
            description=desc, why=why, ev=ev, status=status, source=src,
            created_at=_iso(age),
            started_at=_iso(age - 0.1) if is_run else "",
            ended_at=_iso(age - 0.35) if status in
            ("success", "failed", "unclear") else "",
            results_vs_baseline=(
                f"{acc:.3f} at5_acc vs 0.412 baseline "
                f"({'+' if delta >= 0 else ''}{delta:.3f})"
                if status in ("success", "failed", "unclear") else ""),
            analysis=("Clear, stable gain; the frozen backbone fully avoids "
                      "interference on prior identities."
                      if status == "success" and not is_baseline else
                      "Within seed noise of baseline - inconclusive."
                      if status == "unclear" else
                      "Cosine gate rejected too many updates; net plasticity "
                      "loss outweighed the stability benefit."
                      if status == "failed" else ""),
            conclusion=("Keep - promising direction, worth combining with "
                        "memory." if status == "success" and not is_baseline
                        else "Discard." if status == "failed" else ""),
            hpps={"lr": 1e-4, "n_pert": 100, "batch_size": 1024,
                  "depth": 8} if is_run else {}))

        if is_run:
            run_status = {"success": "kept", "failed": "discarded",
                          "unclear": "discarded", "running": "running"}[status]
            db.add(Run(
                id=iid, project_id=PROJECT_ID, idea_id=f"idea-{iid}",
                run_name=iid, status=run_status, is_baseline=is_baseline,
                gpu_index=gpu, tmux_session=f"train-gpu{gpu}",
                git_commit=f"{_rng.randrange(16**7):07x}",
                config={"lr": 1e-4, "n_pert": 100, "batch_size": 1024,
                        "depth": 8, "solver": "spsa"},
                headline_metric=acc if status != "running" else None,
                baseline_delta=delta if status != "running" else None,
                peak_vram_mb=vram,
                started_at=(_iso(0.03 + 0.012 * n) if status == "running"
                            else _iso(age - 0.1)),
                ended_at=_iso(age - 0.35) if status != "running" else "",
                created_at=_iso(age)))
            if status == "running":
                _curve(iid, 130, acc_final=0.30 + ev, loss_final=0.7)
            else:
                _curve(iid, 300, acc_final=acc, loss_final=0.45 + delta * -1)

    for ev in [
        ("breakthrough", "info", "agent",
         "jepa-frozen-adapter beat baseline by +0.119 at5_acc - new best.", 4.1),
        ("run_finished", "info", "agent",
         "grad-agreement-filter finished: discarded (-0.020 vs baseline).", 3.4),
        ("idea_added", "info", "agent",
         "Added 3 new ideas from analysis of the JEPA run.", 2.9),
        ("run_started", "info", "system",
         "Launched proto-metric-insertion on GPU 0.", 1.2),
        ("run_started", "info", "system",
         "Launched dual-fast-slow-memory on GPU 1.", 1.1),
        ("run_started", "info", "system",
         "Launched hypernet-lora-adapter on GPU 2.", 0.9),
        ("run_started", "info", "system",
         "Launched trust-region-gated-update on GPU 3.", 0.6),
    ]:
        db.add(Event(id=f"ev-{_rng.randrange(16**8):08x}", type=ev[0],
                     severity=ev[1], actor=ev[2], message=ev[3],
                     created_at=_iso(ev[4])))

    for role, content, age in [
        ("researcher", "How's it going? Anything promising?", 0.3),
        ("agent", "Going well. JEPA + frozen adapter is the clear leader so "
         "far (+0.119 at5_acc). All 4 GPUs are busy with the next batch: "
         "prototype insertion, dual fast/slow memory, a hypernet-LoRA adapter, "
         "and a trust-region gated update. Prototype insertion is looking "
         "strong early.", 0.29),
        ("researcher", "Prioritise the meta-selective-plasticity idea next.",
         0.1),
        ("agent", "Got it - pinned meta-selective-plasticity to the top of "
         "the queue. It'll launch as soon as a GPU frees up.", 0.09),
    ]:
        db.add(ChatMessage(id=f"cm-{_rng.randrange(16**8):08x}", role=role,
                           content=content, created_at=_iso(age)))

    for d, title, body, age in [
        ("Day 1", "Setup & baseline",
         "Created the bs1learning repo, wrote train.py for batch-size-1 "
         "continual learning on LFW, and seeded ideas.md. Baseline landed at "
         "0.412 at5_acc - the number to beat.", 6),
        ("Day 3", "Memory beats gradients",
         "Two memory-based directions (in-context episodic memory, +0.066; "
         "JEPA frozen adapter, +0.119) clearly outperformed gradient-surgery "
         "approaches. Gradient-agreement filtering actually regressed "
         "(-0.020): the cosine gate is too aggressive. Pivoting compute toward "
         "memory + adapter hybrids.", 3),
        ("Day 6", "Four-way exploration",
         "All 4 GPUs running hybrids that combine a frozen representation with "
         "a lightweight per-example substrate. Early signal: prototype "
         "insertion in a whitened embedding space is tracking above the JEPA "
         "adapter at the same step count.", 0.2),
    ]:
        db.add(JournalEntry(id=f"jr-{_rng.randrange(16**8):08x}", date=d,
                            title=title, body=body, created_at=_iso(age)))

    db.commit()
    db.close()
