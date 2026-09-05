"""Regression tests for the desktop research-progress frontier."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[2]


def test_frontier_carries_incumbent_across_non_frontier_runs():
    """Replicates/discards must not punch NaN gaps into the green path."""
    src = (ROOT / "backend" / "app" / "static" / "app.js").read_text()
    start = src.index("function frontier(runs)")
    end = src.index("/* A probe / smoke run", start)
    fn = src[start:end]
    runs = [
        {"status": "kept_novel", "headline_metric": 0.5},
        {"status": "kept_replicate", "headline_metric": 0.4},
        {"status": "discarded", "headline_metric": 0.45},
        {"status": "kept_novel", "headline_metric": 0.7},
        {"status": "running", "headline_metric": None},
    ]
    script = (
        "const better=(a,b)=>a>b;\n" + fn + "\n" +
        f"const out=frontier({json.dumps(runs)});" +
        "console.log(JSON.stringify(out.map(r=>r._best)));"
    )
    result = subprocess.run(["node", "-e", script], check=True,
                            text=True, capture_output=True)
    assert json.loads(result.stdout) == [0.5, 0.5, 0.5, 0.7, 0.7]
