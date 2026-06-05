"""Parse + evaluate the user's free-text "Run kill criteria" policy.

The user types a free-form string in the onboarding / settings form (e.g.
``"1 hour OR val_loss plateaus for 500 steps"``) and this module turns it
into structured ``Rule`` objects and decides, per reconciler tick, whether
a given live run should be killed.

Design goals:
  * Permissive parser — accept the most natural phrasings the user might
    type ("1 hour", "1h", "kill after 1 hour", "60 minutes"). Each rule
    is independent so the user can mix and match with ``OR`` / commas /
    ``;`` / newlines.
  * Cheap evaluation — every reconciler tick calls :func:`check_run` on
    every running run. It must do bounded work: one DB-free reduction
    over the already-loaded ``metrics`` dict.
  * Self-contained — the agent gets the original free-text string in
    ``$ARUI_KILL_CRITERIA`` so it knows the policy too.

Supported rule shapes:

  * Time:    ``"1 hour"``, ``"2 hours"``, ``"30 minutes"``, ``"1h"``,
             ``"kill after 1 hour"``, ``"after N seconds"``
  * Steps:   ``"1000 steps"``, ``"kill after 5000 steps"``
  * Plateau: ``"val_loss plateaus for 500 steps"`` (no improvement in N
             steps; direction inferred from metric name)
  * Threshold:
             ``"val_loss > 5.0 for 100 steps"``,
             ``"val_loss above 5 for 100 steps"`` (≥ / ≤ / </> / = also OK)

Multiple rules can be OR'd with ``OR``, ``or``, ``,``, ``;``, newlines —
any of them firing kills the run.

The exact text the user typed is ``parse``'d into a list[Rule]; an
empty list means "no policy" (treated as never-kill).
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field


# ─────────────────────────── rule dataclasses ──────────────────────────────

@dataclass
class TimeRule:
    """Kill after N seconds of wall-clock since the run started."""
    seconds: float

    @property
    def hours(self) -> float:
        return self.seconds / 3600.0

    def describe(self) -> str:
        if self.seconds >= 3600:
            return f"running > {self.hours:g}h"
        if self.seconds >= 60:
            return f"running > {self.seconds / 60:g}min"
        return f"running > {self.seconds:g}s"


@dataclass
class StepRule:
    """Kill once the run has logged N or more steps."""
    steps: int

    def describe(self) -> str:
        return f"step count >= {self.steps}"


@dataclass
class PlateauRule:
    """Kill if `metric` has not improved (per its direction) for N steps."""
    metric: str
    steps: int
    direction: str = "auto"   # "minimize" | "maximize" | "auto"

    def describe(self) -> str:
        return f"{self.metric} plateau for {self.steps} steps"


@dataclass
class ThresholdRule:
    """Kill if `metric op value` has held for N consecutive logged steps."""
    metric: str
    op: str         # ">", ">=", "<", "<=", "==", "!="
    value: float
    steps: int = 1  # how many consecutive steps the condition has to hold

    def describe(self) -> str:
        return f"{self.metric} {self.op} {self.value:g} for {self.steps} steps"


Rule = TimeRule | StepRule | PlateauRule | ThresholdRule


# ──────────────────────────────── parser ───────────────────────────────────

# Word → seconds for time-unit rules.
_TIME_UNITS = {
    "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
    "d": 86400, "day": 86400, "days": 86400,
}

# Operator aliases for threshold rules.
_OPS = {
    ">": ">", "above": ">", "greater than": ">", "gt": ">",
    ">=": ">=", "at least": ">=", "ge": ">=", "≥": ">=",
    "<": "<", "below": "<", "less than": "<", "lt": "<",
    "<=": "<=", "at most": "<=", "le": "<=", "≤": "<=",
    "==": "==", "=": "==", "eq": "==", "equal to": "==", "equals": "==",
    "!=": "!=", "ne": "!=",
}


def _split_clauses(text: str) -> list[str]:
    """Split the free-text into individual rule clauses on OR / , / ; / \\n."""
    # Replace OR / or with a comma, then split.
    t = re.sub(r"\b[Oo][Rr]\b", ",", text)
    parts = re.split(r"[,;\n]+", t)
    return [p.strip() for p in parts if p.strip()]


def _try_time(clause: str) -> Rule | None:
    """Try to read the clause as a wall-clock duration. Accepts numbers
    fused to the unit (``1h``) and spaced (``1 hour``)."""
    # ``1h`` / ``2.5h`` / ``30m`` — number + unit-letter, no space.
    m = re.search(r"(?<![A-Za-z_])(\d+(?:\.\d+)?)\s*([a-zA-Z]+)\b", clause)
    if not m:
        return None
    num_s, unit_s = m.group(1), m.group(2).lower()
    if unit_s not in _TIME_UNITS:
        return None
    # Reject if the clause also mentions "step" — that's a step rule, not time.
    if "step" in clause.lower():
        return None
    try:
        n = float(num_s)
    except ValueError:
        return None
    return TimeRule(seconds=n * _TIME_UNITS[unit_s])


def _try_step(clause: str) -> Rule | None:
    """``500 steps`` / ``after 1000 steps`` / ``kill after N steps``."""
    c = clause.lower()
    if "plateau" in c or "improve" in c:
        return None        # that's a PlateauRule
    if any(op in c for op in (">", "<", "=", "above", "below", "at least",
                              "at most", "greater", "less", "equal")):
        # threshold rule; let _try_threshold pick it up
        return None
    m = re.search(r"(\d+)\s*step", c)
    if not m:
        return None
    try:
        n = int(m.group(1))
    except ValueError:
        return None
    if n <= 0:
        return None
    return StepRule(steps=n)


def _try_plateau(clause: str) -> Rule | None:
    """``val_loss plateaus for 500 steps`` /
    ``val_loss has not improved for 500 steps``."""
    c = clause.lower()
    if "plateau" not in c and "not improv" not in c and "no improv" not in c:
        return None
    # Window length.
    m = re.search(r"for\s+(\d+)\s*step", c)
    if not m:
        m = re.search(r"(\d+)\s*step", c)
    if not m:
        return None
    steps = int(m.group(1))
    if steps <= 0:
        return None
    # Metric name: last meaningful identifier before "plateau" / "improv".
    # We strip "kill after" / "kill" and skip the small set of English
    # stop-words the user might naturally drop in ("has", "have", "is",
    # etc.) so phrasings like "val_acc has not improved" pick val_acc,
    # not "has".
    head = re.split(r"plateau|not improv|no improv", c, 1)[0]
    head = head.replace("kill after", " ").replace("kill", " ")
    tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", head)
    _STOP = {"has", "have", "had", "is", "was", "the", "a", "an", "if",
             "when", "after", "for", "kill", "and", "or", "metric"}
    meaningful = [t for t in tokens if t.lower() not in _STOP]
    metric = (meaningful[-1] if meaningful
              else (tokens[-1] if tokens else "val_loss"))
    return PlateauRule(metric=metric, steps=steps)


def _try_threshold(clause: str) -> Rule | None:
    """``val_loss > 5.0 for 100 steps`` (steps optional, default 1)."""
    c = clause.strip()
    cl = c.lower()
    # Skip if this is plainly a plateau / pure time rule.
    if "plateau" in cl or "not improv" in cl or "no improv" in cl:
        return None
    # Pick the operator. Try the symbolic forms first since they're
    # unambiguous; fall back to the worded aliases.
    op_re = (r"(>=|<=|==|!=|≥|≤|>|<|="
             r"|\bat least\b|\bat most\b"
             r"|\bgreater than\b|\bless than\b"
             r"|\bequal to\b|\bequals\b"
             r"|\babove\b|\bbelow\b)")
    m = re.search(op_re, cl)
    if not m:
        return None
    op_word = m.group(1).strip()
    op = _OPS.get(op_word)
    if not op:
        return None
    # The metric is the last identifier before the operator.
    head = cl[:m.start()]
    head_tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", head)
    if not head_tokens:
        return None
    metric = head_tokens[-1]
    # The value comes right after the operator.
    tail = cl[m.end():]
    vm = re.search(r"(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)", tail)
    if not vm:
        return None
    try:
        value = float(vm.group(1))
    except ValueError:
        return None
    # Optional "for N steps" suffix.
    steps_m = re.search(r"for\s+(\d+)\s*step", tail)
    steps = int(steps_m.group(1)) if steps_m else 1
    if steps <= 0:
        steps = 1
    return ThresholdRule(metric=metric, op=op, value=value, steps=steps)


_PARSERS = (_try_plateau, _try_threshold, _try_step, _try_time)


def parse(text: str) -> list[Rule]:
    """Parse ``text`` into a list of rules. Empty list = no policy.

    Permissive: unrecognised clauses are silently skipped (so a typo in
    one clause doesn't disable the rest of the policy).
    """
    if not text or not text.strip():
        return []
    out: list[Rule] = []
    for clause in _split_clauses(text):
        for fn in _PARSERS:
            try:
                r = fn(clause)
            except Exception:
                r = None
            if r is not None:
                out.append(r)
                break
    return out


# ──────────────────────────── evaluator helpers ────────────────────────────


def _direction_for(metric: str) -> str:
    """Cheap higher-is-better-vs-lower-is-better heuristic — same shape as
    the one in api.py so the plateau rule does the right thing without
    needing the project row."""
    ml = re.sub(r"[\s\-]+", "_", (metric or "").strip().lower())
    minimize = ("loss", "perplexity", "ppl", "error", "rmse", "mse", "mae",
                "bpb", "bpc", "fid", "kid", "divergence", "regret")
    if any(t in ml for t in minimize):
        return "minimize"
    maximize = ("accuracy", "_acc", "acc_", "acc@", "f1", "exact_match", "em",
                "_em", "bleu", "rouge", "meteor", "chrf", "score", "reward",
                "auc", "map", "ndcg", "hit", "mrr", "pass@", "win", "elo")
    if any(t in ml for t in maximize):
        return "maximize"
    return "minimize"


def _op_holds(op: str, a: float, b: float) -> bool:
    if op == ">":  return a > b
    if op == ">=": return a >= b
    if op == "<":  return a < b
    if op == "<=": return a <= b
    if op == "==": return a == b
    if op == "!=": return a != b
    return False


def _series_for(metrics: dict, key: str) -> list[tuple[float, float]]:
    """Return [(step, value), ...] for the metric, sorted by step.

    Accepts either the ``query`` shape (``{key: [[s,v], ...]}``) or the
    raw ``points`` shape (``[{"key": k, "step": s, "value": v}, ...]``).
    """
    if not metrics:
        return []
    if isinstance(metrics, dict):
        raw = metrics.get(key)
        if raw is None:
            return []
        out = []
        for row in raw:
            try:
                if isinstance(row, dict):
                    out.append((float(row.get("step", 0) or 0),
                                float(row.get("value", 0) or 0)))
                else:
                    out.append((float(row[0]), float(row[1])))
            except (TypeError, ValueError, IndexError):
                continue
        out.sort(key=lambda p: p[0])
        return out
    return []


# ──────────────────────────────── checker ──────────────────────────────────


def check_run(run, rules: list[Rule], metrics: dict | None = None,
              now: float | None = None) -> tuple[bool, str]:
    """Evaluate the rule list against a live run.

    Args:
      run: a Run-shaped object — anything with ``started_at`` (ISO),
           ``id``, ``run_name``, and (optionally) ``config``. The
           reconciler passes the ORM row directly.
      rules: the parsed rules. Empty list -> never kill.
      metrics: ``{key: [[step, value], ...]}`` for the run. Caller is
           responsible for supplying this (typically ``metrics.query``).
           May be ``None`` -> step/plateau/threshold rules are no-ops.
      now: unix-epoch override (test hook). Defaults to ``time.time()``.

    Returns ``(should_kill, reason)``. ``reason`` is a short
    human-readable description suitable for the crash Event message.
    """
    if not rules:
        return (False, "")
    now = now if now is not None else time.time()
    metrics = metrics or {}
    for rule in rules:
        try:
            fired, why = _evaluate(rule, run, metrics, now)
        except Exception as e:                                # noqa: BLE001
            print(f"[kill_criteria] rule {rule!r} eval error: {e}",
                  flush=True)
            continue
        if fired:
            return (True, why)
    return (False, "")


def _evaluate(rule: Rule, run, metrics: dict,
              now: float) -> tuple[bool, str]:
    if isinstance(rule, TimeRule):
        start = _started_epoch(run)
        if start is None:
            return (False, "")
        if (now - start) >= rule.seconds:
            return (True, f"kill rule: {rule.describe()}")
        return (False, "")

    if isinstance(rule, StepRule):
        last = _max_step(metrics)
        if last is None:
            return (False, "")
        if last >= rule.steps:
            return (True, f"kill rule: {rule.describe()} (at step {last})")
        return (False, "")

    if isinstance(rule, PlateauRule):
        series = _series_for(metrics, rule.metric)
        if not series:
            return (False, "")
        last_step, _ = series[-1]
        direction = rule.direction
        if direction == "auto":
            direction = _direction_for(rule.metric)
        # Best value in the series + the step at which it occurred.
        if direction == "maximize":
            best_step, _ = max(series, key=lambda p: (p[1], -p[0]))
        else:
            best_step, _ = min(series, key=lambda p: (p[1], -p[0]))
        if (last_step - best_step) >= rule.steps:
            return (True,
                    f"kill rule: {rule.describe()} "
                    f"(best at step {int(best_step)}, "
                    f"now at step {int(last_step)})")
        return (False, "")

    if isinstance(rule, ThresholdRule):
        series = _series_for(metrics, rule.metric)
        if not series:
            return (False, "")
        # Count consecutive trailing steps where the condition holds.
        trailing = 0
        for _, v in reversed(series):
            if _op_holds(rule.op, v, rule.value):
                trailing += 1
            else:
                break
        if trailing >= rule.steps:
            return (True,
                    f"kill rule: {rule.describe()} "
                    f"(condition held for {trailing} consecutive logs)")
        return (False, "")

    return (False, "")


# ──────────────────────────── helpers used above ──────────────────────────


def _started_epoch(run) -> float | None:
    """Best-effort: read ``run.started_at`` (ISO) or ``run.created_at`` and
    return its epoch. Returns ``None`` if neither is parseable."""
    import datetime as dt
    for attr in ("started_at", "created_at"):
        raw = getattr(run, attr, None)
        if not raw:
            continue
        try:
            d = dt.datetime.fromisoformat(raw)
        except (TypeError, ValueError):
            continue
        if d.tzinfo is None:
            d = d.replace(tzinfo=dt.timezone.utc)
        return d.timestamp()
    return None


def _max_step(metrics: dict) -> int | None:
    """The highest step recorded across any key in the metrics dict."""
    if not metrics:
        return None
    best: float | None = None
    for raw in metrics.values():
        for row in (raw or []):
            try:
                s = float(row[0] if not isinstance(row, dict)
                          else row.get("step", 0))
            except (TypeError, ValueError, IndexError):
                continue
            if best is None or s > best:
                best = s
    return int(best) if best is not None else None
