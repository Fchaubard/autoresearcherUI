"""Email notifications for autoresearcherUI.

Driven by the onboarding ``cadence`` setting:

  * ``immediate``  - email the moment a run beats the project's best metric
  * ``<N>h``       - a digest every N hours (1h/4h/12h/24h): what was tried and
                     how it scored, what is training now and a rough ETA, and
                     what ideas are next on deck
  * ``off``        - nothing is sent (the "infinite" / silent option)

Delivery auto-detects a transport from the onboarding config: a Resend API key
if one is present, otherwise SMTP (Gmail or any provider). Everything here is
best-effort and never raises into a request path.
"""
from __future__ import annotations

import base64
import datetime as dt
import html as _html
import json
import smtplib
import ssl
import threading
import time
import urllib.request
from email.message import EmailMessage

from .config import DATA_DIR
from .db import SessionLocal
from .models import Project, Run, Setting


# ───────────────────────────── config helpers ──────────────────────────────

def _cfg() -> dict:
    """The onboarding config dict (holds email, cadence and email creds)."""
    db = SessionLocal()
    try:
        row = db.query(Setting).filter(Setting.key == "onboarding").first()
        return dict(row.value) if row and isinstance(row.value, dict) else {}
    finally:
        db.close()


def _cadence(cfg: dict) -> str:
    return str(cfg.get("cadence") or "off").strip().lower()


def _dashboard_url(cfg: dict) -> str:
    return (cfg.get("dashboard_url") or "").strip().rstrip("/")


def _cadence_hours(cad: str) -> float | None:
    """Hours for a periodic digest, or None for off / immediate."""
    if cad.endswith("h"):
        try:
            return float(cad[:-1])
        except ValueError:
            return None
    return None


def _parse_iso(s: str | None):
    try:
        d = dt.datetime.fromisoformat(s) if s else None
    except Exception:
        return None
    if d is not None and d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d


def _fmt_dur(secs: float) -> str:
    secs = int(max(0, secs))
    h, m = secs // 3600, (secs % 3600) // 60
    return f"{h}h{m:02d}m" if h else f"{m}m"


# ────────────────────────────── delivery ───────────────────────────────────

def _recipients(cfg: dict) -> list[str]:
    """The cleaned recipient list from email_recipients (comma-separated), or
    the sender address as a fallback. Tolerates quotes, spaces, empty entries
    and a stray trailing comma."""
    out: list[str] = []
    for part in str(cfg.get("email_recipients") or "").replace(";",
                                                               ",").split(","):
        addr = part.strip().strip("'\"").strip()
        if "@" in addr and addr not in out:
            out.append(addr)
    if not out:
        single = (cfg.get("email") or "").strip().strip("'\"")
        if "@" in single:
            out = [single]
    return out


def _emit_smtp_failure_event(detail: str) -> None:
    """Persist an SMTP failure as an Event so the user sees that emails
    aren't going out (without having to scroll through backend logs).
    Deduplicates per hour so a broken Gmail password doesn't spam the
    Summary feed."""
    try:
        import datetime as _dt
        import os as _os
        from .db import SessionLocal
        from .models import Event
        db = SessionLocal()
        try:
            cutoff = (_dt.datetime.now(_dt.timezone.utc)
                      - _dt.timedelta(hours=1)).isoformat()
            recent = (db.query(Event)
                      .filter(Event.type == "email_failed")
                      .filter(Event.created_at > cutoff).first())
            if recent:
                return
            ev = Event(id=f"ev-{_os.urandom(4).hex()}",
                       type="email_failed",
                       severity="warning", actor="notify",
                       message=("Email digest failed to send: "
                                f"{detail[:200]}. Fix the SMTP settings "
                                "in onboarding (most often: Gmail app "
                                "password expired or 2FA was turned off)."),
                       created_at=_dt.datetime.now(
                           _dt.timezone.utc).isoformat())
            db.add(ev)
            db.commit()
            try:
                from .bus import bus
                bus.publish("events", "event", ev.dict())
            except Exception:
                pass
        finally:
            db.close()
    except Exception as e:                           # noqa: BLE001
        print(f"[notify] _emit_smtp_failure_event failed: {e}", flush=True)


def _smtp_send(host, port, user, password, sender, recipients, subject,
               text, html=None, images=None) -> bool:
    try:
        msg = EmailMessage()
        msg["From"] = sender or user or "autoresearcher@localhost"
        msg["To"] = ", ".join(recipients)
        msg["Subject"] = subject
        msg.set_content(text or "")
        if html:
            msg.add_alternative(html, subtype="html")
            if images:
                part = msg.get_payload()[-1]          # the text/html part
                for cid, png in images.items():
                    part.add_related(png, "image", "png", cid=cid)
        with smtplib.SMTP(host, port, timeout=45) as s:
            s.ehlo()
            try:
                s.starttls(context=ssl.create_default_context())
                s.ehlo()
            except smtplib.SMTPException:
                pass                                  # server without STARTTLS
            if user:
                s.login(user, password)
            s.send_message(msg)
        print(f"[notify] smtp -> {recipients} via {host}", flush=True)
        return True
    except smtplib.SMTPAuthenticationError as e:     # noqa: BLE001
        print(f"[notify] smtp auth error: {e}", flush=True)
        _emit_smtp_failure_event(f"auth rejected (535): {e}")
        return False
    except Exception as e:                           # noqa: BLE001
        print(f"[notify] smtp error: {e}", flush=True)
        _emit_smtp_failure_event(str(e))
        return False


def _resend_send(api_key, sender, recipients, subject, text, html=None,
                 images=None) -> bool:
    payload = {"from": sender or "autoresearcherUI <onboarding@resend.dev>",
               "to": recipients, "subject": subject, "text": text}
    if html:
        payload["html"] = html
    if images:
        payload["attachments"] = [
            {"filename": f"{cid}.png",
             "content": base64.b64encode(png).decode(), "content_id": cid}
            for cid, png in images.items()]
    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            ok = r.status in (200, 201, 202)
            print(f"[notify] resend -> {recipients}: HTTP {r.status}",
                  flush=True)
            return ok
    except Exception as e:                           # noqa: BLE001
        print(f"[notify] resend error: {e}", flush=True)
        return False


def _deliver(subject, text, cfg, html=None, images=None) -> bool:
    """Send via whichever transport is configured. Gmail (an app password on
    the sender address) is the primary path; Resend and generic SMTP are
    supported fallbacks. Email is optional — with nothing configured this is a
    graceful no-op."""
    recipients = _recipients(cfg)
    if not recipients:
        print("[notify] no recipients configured - skipping", flush=True)
        return False
    sender = (cfg.get("email") or "").strip().strip("'\"")

    gmail_pw = str(cfg.get("gmail_app_pw") or "").strip().replace(" ", "")
    if gmail_pw and sender:
        return _smtp_send("smtp.gmail.com", 587, sender, gmail_pw, sender,
                          recipients, subject, text, html, images)

    host = (cfg.get("smtp_host") or "").strip()
    if host:
        port = int(str(cfg.get("smtp_port") or "587").strip() or "587")
        return _smtp_send(host, port, cfg.get("smtp_user") or "",
                          cfg.get("smtp_pass") or "",
                          cfg.get("notify_from") or cfg.get("smtp_user")
                          or sender, recipients, subject, text, html, images)

    api_key = (cfg.get("resend_api_key") or "").strip()
    if api_key:
        return _resend_send(api_key, cfg.get("notify_from") or "",
                            recipients, subject, text, html, images)

    print("[notify] no email transport configured "
          "(set a Gmail app password) - email not sent", flush=True)
    return False


def emails_paused() -> bool:
    """Return True iff the user clicked 'Pause all emails' in Settings.

    Single source of truth read by every email path. Stored on the
    onboarding row (`emails_paused: bool`). Default False — emails go
    out as configured."""
    try:
        cfg = _cfg() or {}
        return bool(cfg.get("emails_paused"))
    except Exception:                                       # noqa: BLE001
        return False


def research_paused() -> bool:
    """Return True iff the user clicked 'Pause research' in Settings.

    Single source of truth read by the orchestrator (skips launching new
    runs), the PI agent (skips nudging the research / author agent), and
    /api/track/run (rejects new runs with 423). Stored on the onboarding
    row (`research_paused: bool`). Default False — research runs as
    configured. Gate is intentionally co-located with `emails_paused`
    because both are pause flags on the onboarding settings row read by
    multiple subsystems."""
    try:
        cfg = _cfg() or {}
        return bool(cfg.get("research_paused"))
    except Exception:                                       # noqa: BLE001
        return False


def send(subject, text, html=None, images=None) -> bool:
    """Send a notification to the configured recipients. True on success.

    Returns False (skipped) without attempting delivery when the user
    has paused emails in Settings. This single check covers ALL email
    paths — research digests, paper digests, token failures, system
    warnings — because every notification eventually calls send()."""
    if emails_paused():
        print("[notify] emails paused by user — skipping send", flush=True)
        return False
    return _deliver(subject, text, _cfg(), html, images)


# ──────────────────────────── HTML emails ──────────────────────────────────

def _safe_charts():
    try:
        from . import charts
        return charts
    except Exception as e:                           # noqa: BLE001
        print(f"[notify] charts unavailable: {e}", flush=True)
        return None


def _esc(s) -> str:
    return _html.escape(str(s if s is not None else ""))


def _stat_cards(pairs) -> str:
    cells = ""
    for label, value in pairs:
        cells += (
            '<td style="background:#1d2127;border:1px solid #23272E;'
            'border-radius:9px;padding:9px 6px;text-align:center;">'
            f'<div style="font-size:9.5px;color:#5C636B;'
            'text-transform:uppercase;letter-spacing:.5px;">'
            f'{_esc(label)}</div>'
            '<div style="font-size:16px;font-weight:700;color:#E6E8EB;'
            f'margin-top:3px;">{_esc(value)}</div></td>'
            '<td style="width:7px;"></td>')
    return ('<table role="presentation" style="width:100%;'
            f'border-collapse:collapse;margin:4px 0 14px;"><tr>{cells}</tr>'
            '</table>')


def _section(heading, items) -> str:
    lis = "".join(
        f'<li style="margin:3px 0;">{_esc(x)}</li>' for x in items) \
        or '<li style="color:#5C636B;list-style:none;">—</li>'
    return (f'<div style="font-size:11px;color:#6366F1;font-weight:700;'
            'text-transform:uppercase;letter-spacing:.5px;margin:16px 0 5px;">'
            f'{_esc(heading)}</div>'
            f'<ul style="margin:0;padding-left:18px;color:#C7CAD0;'
            f'font-size:12.5px;line-height:1.6;">{lis}</ul>')


def _img(cid, alt) -> str:
    return (f'<img src="cid:{cid}" alt="{_esc(alt)}" '
            'style="width:100%;border-radius:10px;border:1px solid #23272E;'
            'margin:8px 0;display:block;"/>')


def _shell(subtitle, body_html, dashboard_url) -> str:
    btn = ""
    if dashboard_url:
        btn = (f'<a href="{_esc(dashboard_url)}" '
               'style="display:block;text-align:center;background:#6366F1;'
               'color:#ffffff;text-decoration:none;font-weight:600;'
               'font-size:14px;padding:13px;border-radius:10px;'
               'margin:18px 0 2px;">Open the dashboard &rarr;</a>')
    return (
        '<!doctype html><html><body style="margin:0;padding:16px 10px;'
        "background:#0f1115;font-family:-apple-system,BlinkMacSystemFont,"
        "'Segoe UI',Roboto,Helvetica,Arial,sans-serif;\">"
        '<div style="max-width:600px;margin:0 auto;background:#14171C;'
        'border-radius:14px;overflow:hidden;border:1px solid #23272E;">'
        '<div style="background:#0B0D10;padding:17px 22px;">'
        '<div style="font-size:17px;font-weight:700;color:#E6E8EB;">'
        'autoresearcher<span style="color:#6366F1;">UI</span></div>'
        '<div style="font-size:12.5px;color:#9BA1A8;margin-top:3px;">'
        f'{_esc(subtitle)}</div></div>'
        '<div style="padding:18px 22px;color:#C7CAD0;font-size:13.5px;'
        f'line-height:1.6;">{body_html}{btn}</div>'
        '<div style="padding:11px 22px;background:#0B0D10;font-size:10.5px;'
        'color:#5C636B;">autoresearcherUI — autonomous research, on your own '
        'GPUs.</div></div></body></html>')


# ───────────────────────────── metric helpers ──────────────────────────────

def _baseline_metric(db, proj) -> float | None:
    b = (db.query(Run).filter(Run.is_baseline.is_(True))
         .filter(Run.headline_metric.isnot(None)).first())
    return b.headline_metric if b else None


def _best_run(db, proj):
    maximize = proj.metric_direction == "maximize"
    kept = [r for r in db.query(Run).all()
            if r.status == "kept" and r.headline_metric is not None]
    if not kept:
        return None
    return (max if maximize else min)(kept, key=lambda r: r.headline_metric)


# ──────────────────────── immediate-on-improvement ─────────────────────────

def on_run_finished(run_id: str) -> None:
    """Called after a run finishes. If cadence is 'immediate' and this run set
    a new project-best metric, email straight away. Safe to call in a thread."""
    try:
        cfg = _cfg()
        if _cadence(cfg) != "immediate":
            return
        db = SessionLocal()
        try:
            run = db.query(Run).filter(Run.id == run_id).first()
            proj = db.query(Project).first()
            if (not run or not proj or run.status == "crashed"
                    or run.headline_metric is None):
                return
            maximize = proj.metric_direction == "maximize"
            others = [r.headline_metric for r in db.query(Run).all()
                      if r.id != run_id and r.status != "crashed"
                      and r.headline_metric is not None]
            is_best = all(
                (run.headline_metric > o) if maximize
                else (run.headline_metric < o) for o in others)
            if not is_best:
                return
            metric = proj.validation_metric or "metric"
            pname = proj.name
            hm = run.headline_metric
            rname = run.run_name or run.id
            base = _baseline_metric(db, proj)
        finally:
            db.close()
        subject = f"[{pname}] new best {metric} = {hm:.4f}"
        lines = [f"A new best result just landed on '{pname}'.", "",
                 f"  run:       {rname}", f"  {metric}:  {hm:.6f}"]
        cards = [("new best", f"{hm:.4f}")]
        if base is not None:
            diff = hm - base
            good = (diff > 0) == maximize
            lines += [f"  baseline:  {base:.6f}",
                      f"  vs base:   {'+' if diff >= 0 else ''}{diff:.6f} "
                      f"({'better' if good else 'worse'})"]
            cards += [("baseline", f"{base:.4f}"),
                      ("vs baseline", f"{'+' if good else '-'}{abs(diff):.4f}")]
        lines += ["", "- autoresearcherUI"]
        images = {}
        ch = _safe_charts()
        if ch:
            png = ch.progress_png()
            if png:
                images["progress"] = png
        body = (f'<p style="margin:0 0 4px;">A new best result just landed on '
                f'<b style="color:#E6E8EB;">{_esc(pname)}</b>.</p>'
                f'<p style="margin:0 0 8px;color:#34D399;font-weight:600;'
                f'font-size:15px;">{_esc(metric)} = {hm:.6f}'
                f'<span style="color:#9BA1A8;font-weight:400;font-size:12px;">'
                f' &nbsp;{_esc(rname)}</span></p>'
                + _stat_cards(cards)
                + (_img("progress", "progress") if "progress" in images
                   else ""))
        html = _shell(f"New best — {metric} {hm:.4f}", body,
                      _dashboard_url(_cfg()))
        send(subject, "\n".join(lines), html, images)
    except Exception as e:                           # noqa: BLE001
        print(f"[notify] on_run_finished error: {e}", flush=True)


# ───────────────────────────── periodic digest ─────────────────────────────

_PENDING = ("pending", "todo", "queued", "planned", "next", "on deck")


def _ideas_on_deck(cfg: dict, proj) -> list[str]:
    """Best-effort: pull not-yet-run ideas from the agent's ideas.md. Handles
    both markdown-table and bullet-list layouts; tables win when present."""
    name = (cfg.get("repo_name") or (proj.name if proj else "") or "").strip()
    path = DATA_DIR / "workspace" / name / "ideas.md"
    if not path.exists():
        return []
    table: list[str] = []
    bullets: list[str] = []
    for ln in path.read_text(errors="ignore").splitlines():
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("|"):                          # markdown table row
            cells = [c.strip() for c in s.strip("|").split("|")]
            if len(cells) < 2 or all(set(c) <= set("-: ") for c in cells):
                continue                               # separator row
            status = cells[0].lower()
            if status in ("status", "state"):
                continue                               # header row
            if any(w in status for w in _PENDING):
                idea = cells[1] if len(cells) > 1 else ""
                what = cells[-1] if len(cells) > 2 else ""
                table.append((f"{idea} — {what}" if what else idea)[:140])
        elif s[0] in "-*•":                            # bullet-list line
            t = s.lstrip("-*• ").strip()
            if t and len(t) > 8 and not any(
                    d in t.lower() for d in ("[x]", "done", "complete")):
                bullets.append(t[:140])
    return (table or bullets)[:8]


def summary_text(window_hours: float):
    """Build the (subject, body) for a periodic digest, or (None, None)."""
    db = SessionLocal()
    try:
        proj = db.query(Project).first()
        if not proj:
            return None, None
        runs = db.query(Run).all()
        now = dt.datetime.now(dt.timezone.utc)
        cutoff = now - dt.timedelta(hours=window_hours)
        metric = proj.validation_metric or "metric"
        maximize = proj.metric_direction == "maximize"
        base = _baseline_metric(db, proj)

        finished, durations = [], []
        for r in runs:
            st, ed = _parse_iso(r.started_at), _parse_iso(r.ended_at)
            if st and ed:
                durations.append((ed - st).total_seconds())
            if r.status in ("kept", "crashed", "discarded") and ed \
                    and ed >= cutoff:
                finished.append((r, ed))
        finished.sort(key=lambda x: x[1])
        running = [r for r in runs if r.status == "running"]
        durations.sort()
        med = durations[len(durations) // 2] if durations else None
        best = _best_run(db, proj)

        L = [f"autoresearcherUI digest - {proj.name}",
             f"Window: last {window_hours:g}h  "
             f"({now.strftime('%Y-%m-%d %H:%M UTC')})", ""]
        if base is not None:
            L.append(f"Baseline {metric}: {base:.6f}")
        if best is not None:
            L.append(f"Best so far:      {best.headline_metric:.6f}  "
                     f"({best.run_name or best.id})")
        L.append("")

        L.append(f"== Tried in the last {window_hours:g}h ({len(finished)}) ==")
        if not finished:
            L.append("  (nothing finished in this window)")
        for r, _ in finished:
            hm = ("-" if r.headline_metric is None
                  else f"{r.headline_metric:.4f}")
            delta = ""
            if r.headline_metric is not None and base is not None:
                diff = r.headline_metric - base
                good = (diff > 0) == maximize
                delta = (f"  ({'+' if diff >= 0 else ''}{diff:.4f} vs base, "
                         f"{'better' if good else 'worse'})")
            L.append(f"  [{r.status:8}] {(r.run_name or r.id):24} "
                     f"{metric}={hm}{delta}")
        L.append("")

        L.append(f"== In progress now ({len(running)}) ==")
        if not running:
            L.append("  (no runs currently training)")
        for r in running:
            st = _parse_iso(r.started_at)
            elapsed = (now - st).total_seconds() if st else 0
            eta = ""
            if med and st:
                rem = med - elapsed
                eta = (f", ~{_fmt_dur(rem)} left"
                       if rem > 0 else ", finishing any moment")
            L.append(f"  {(r.run_name or r.id):24} running "
                     f"{_fmt_dur(elapsed)}{eta}")
        L.append("")

        ideas = _ideas_on_deck(_cfg(), proj)
        L.append("== Next on deck ==")
        if ideas:
            L += [f"  - {x}" for x in ideas[:8]]
        else:
            L.append("  (the agent is choosing its next experiments "
                     "autonomously)")
        L += ["", "- autoresearcherUI"]

        subject = (f"[{proj.name}] {window_hours:g}h digest - "
                   f"{len(finished)} finished, {len(running)} running")
        return subject, "\n".join(L)
    finally:
        db.close()


_STATUS_ICON = {
    "kept": "✓", "success": "✓",
    "discarded": "◯", "failed": "◯",
    "crashed": "✕", "running": "▶", "queued": "·",
}
_STATUS_COLOR = {
    "kept": "#34D399", "success": "#34D399",
    "discarded": "#F87171", "failed": "#F87171",
    "crashed": "#F43F5E", "running": "#FBBF24", "queued": "#A78BFA",
}


def _run_cards_html(runs, metric_name: str, baseline: float | None) -> str:
    """Render Summary-style cards for each completed run in the digest window."""
    if not runs:
        return ""
    rows = []
    for r in runs:
        ic = _STATUS_ICON.get(r.status, "•")
        col = _STATUS_COLOR.get(r.status, "#A78BFA")
        cfg = r.config if isinstance(r.config, dict) else {}
        what = (cfg.get("what") or cfg.get("description") or "").strip()
        why = (cfg.get("why") or "").strip()
        review = cfg.get("review") or {}
        learning = (review.get("learning") or "").strip()
        reviewer = (review.get("reviewer") or "").strip()
        hm = "diverged" if r.status == "crashed" else (
            f"{r.headline_metric:.4f}" if r.headline_metric is not None
            else "—")
        delta = ""
        if (r.headline_metric is not None and baseline is not None
                and r.status != "crashed"):
            d = r.headline_metric - baseline
            sign = "+" if d >= 0 else "−"
            delta = (f'<span style="color:#9BA1A8;font-size:11px;'
                     f'margin-left:8px">{sign}{abs(d):.3f} vs base</span>')
        meta = ((f'<div style="font-size:12px;color:#9BA1A8;'
                 f'margin-top:6px">{_esc(what)}</div>') if what else "") + \
               ((f'<div style="font-size:11px;color:#7a818b;'
                 f'margin-top:3px"><i>why:</i> {_esc(why)}</div>') if why else "")
        rv = ""
        if learning:
            rv = (f'<div style="margin-top:8px;padding:8px 10px;'
                  f'background:#181C22;border-left:2px solid #6366F1;'
                  f'border-radius:5px;font-size:11.5px;color:#C7CCD3;'
                  f'line-height:1.5"><b style="color:#A78BFA">'
                  f'★ Council · {_esc(reviewer or "?")}</b><br>'
                  f'{_esc(learning)}</div>')
        rows.append(
            f'<tr><td style="padding:12px 14px;border-bottom:1px solid #23272E">'
            f'<table width="100%" cellspacing="0" cellpadding="0"><tr>'
            f'<td width="22" style="vertical-align:top;color:{col};'
            f'font-size:14px;font-weight:700">{ic}</td>'
            f'<td style="padding-left:8px">'
            f'<div style="font-family:Menlo,monospace;font-size:12px;'
            f'color:#E6E8EB;font-weight:600">{_esc(r.run_name or r.id)}</div>'
            f'{meta}{rv}</td>'
            f'<td align="right" style="vertical-align:top;'
            f'font-family:Menlo,monospace;font-size:12px;'
            f'color:{col};white-space:nowrap;padding-left:10px">'
            f'{hm}{delta}</td></tr></table></td></tr>')
    return ('<div style="font-size:9px;color:#5C636B;text-transform:uppercase;'
            'letter-spacing:.7px;font-weight:700;margin:18px 0 6px">'
            'Completed experiments this period</div>'
            f'<table width="100%" cellspacing="0" cellpadding="0" '
            f'style="border-collapse:collapse;background:#14171C;'
            f'border:1px solid #23272E;border-radius:8px;overflow:hidden">'
            f'{"".join(rows)}</table>')


def digest_email(window_hours: float):
    """Build the full (subject, text, html, images) for a periodic digest.

    Branches on the project's current mode: paper-mode authors get a
    DIFFERENT digest (claims, decisions waiting, draft status) instead of
    the research-mode "what we tried" feed. Either way, the system-stats
    block (disk / RAM / GPU + warnings like low disk) is appended at the
    bottom so the user sees infra issues without opening the UI."""
    try:
        from . import paper as _paper
        if _paper.project_mode() == "paper":
            return _paper_digest_email(window_hours)
    except Exception as e:                              # noqa: BLE001
        # If paper mode lookup fails for any reason, fall back to the
        # research digest — never lose the digest entirely.
        print(f"[notify] paper mode check failed, "
              f"defaulting to research digest: {e}", flush=True)
    subject, text = summary_text(window_hours)
    if not subject:
        return None, None, None, None
    db = SessionLocal()
    try:
        proj = db.query(Project).first()
        runs = db.query(Run).all()
        metric = proj.validation_metric or "metric"
        base = _baseline_metric(db, proj)
        best = _best_run(db, proj)
        pname = proj.name
        now = dt.datetime.now(dt.timezone.utc)
        cutoff = now - dt.timedelta(hours=window_hours)
        finished, running = [], []
        for r in runs:
            ed = _parse_iso(r.ended_at)
            if r.status in ("kept", "crashed", "discarded", "failed",
                            "success") and ed and ed >= cutoff:
                finished.append(r)
            if r.status == "running":
                running.append(r)
        # newest first — most relevant at the top of the email
        finished.sort(key=lambda r: r.ended_at or "", reverse=True)
    finally:
        db.close()
    ideas = _ideas_on_deck(_cfg(), proj)
    cards = [("baseline", f"{base:.4f}" if base is not None else "—"),
             ("best", f"{best.headline_metric:.4f}" if best else "—"),
             ("done {}h".format(int(window_hours)), str(len(finished))),
             ("running", str(len(running)))]
    images = {}
    # Generate chart pngs FRESH at send time (no caching) so the email reflects
    # what just happened, not an hour-old snapshot.
    ch = _safe_charts()
    if ch:
        try:
            p = ch.progress_png()
            if p:
                images["progress"] = p
        except Exception as e:                          # noqa: BLE001
            print(f"[notify] progress chart error: {e}", flush=True)
        try:
            lp = ch.losses_png()
            if lp:
                images["losses"] = lp
        except Exception as e:                          # noqa: BLE001
            print(f"[notify] losses chart error: {e}", flush=True)
    body = (f'<p style="margin:0 0 10px;">How '
            f'<b style="color:#E6E8EB;">{_esc(pname)}</b> has progressed over '
            f'the last {window_hours:g}h:</p>' + _stat_cards(cards))
    if "progress" in images:
        body += _img("progress", f"{metric} vs experiment — fresh as of "
                                  f"send time")
    body += _run_cards_html(finished[:8], metric, base)
    body += _section("In progress now",
                     [r.run_name or r.id for r in running])
    body += _section("Next on deck", ideas[:8])
    if "losses" in images:
        body += _img("losses", "Recent training curves")
    body += _system_stats_block()
    text += "\n\n" + _system_stats_text()
    html = _shell(f"{window_hours:g}h digest — {pname}", body,
                  _dashboard_url(_cfg()))
    return subject, text, html, images


def send_digest_now() -> bool:
    """Send a digest immediately (used for verification / manual trigger)."""
    if emails_paused():
        print("[notify] digest skipped — emails paused by user",
              flush=True)
        return False
    cfg = _cfg()
    hrs = _cadence_hours(_cadence(cfg)) or 1.0
    subject, text, html, images = digest_email(hrs)
    if not subject:
        return False
    return send(subject, text, html, images)


# ─────────────────────── system stats block (both modes) ──────────────────


_SEV_COLOR = {"critical": "#F43F5E", "warning": "#F59E0B", "info": "#6366F1"}


def _system_snapshot() -> tuple[dict, list[dict]]:
    """Pull the cached host stats + active warnings from monitor/maintenance.
    Safe for both modes; returns ({stats}, [warnings])."""
    try:
        from . import monitor, maintenance
        return monitor.system_stats(), maintenance.system_warnings()
    except Exception as e:                              # noqa: BLE001
        print(f"[notify] system snapshot unavailable: {e}", flush=True)
        return {}, []


def _system_stats_text() -> str:
    """Plain-text version of the host snapshot for the email's text/plain
    alternative."""
    s, warns = _system_snapshot()
    if not s:
        return ""
    L = ["== Node ==",
         f"  CPU:   {s.get('cpu_percent', '?')}%   "
         f"load {' '.join(str(x) for x in s.get('loadavg', []))}"]
    ram = s.get("ram") or {}
    if ram.get("total_gb"):
        L.append(f"  RAM:   {ram.get('used_gb','?')}/{ram['total_gb']} GB "
                 f"({ram.get('percent','?')}%)")
    disk = s.get("disk") or {}
    if disk.get("total_gb"):
        L.append(f"  Disk:  {disk.get('used_gb','?')}/{disk['total_gb']} GB "
                 f"({disk.get('percent','?')}%)   "
                 f"free {disk.get('free_gb','?')} GB")
    gpus = s.get("gpus") or []
    if gpus:
        L.append(f"  GPUs:  {len(gpus)}  "
                 + "  ".join(f"#{g.get('index')}:"
                              f"{int(g.get('util_pct') or 0)}%/"
                              f"{int(g.get('temp_c') or 0)}°C"
                              for g in gpus))
    if warns:
        L.append("")
        L.append("⚠ Warnings:")
        for w in warns:
            L.append(f"  [{w['severity']}] {w['msg']}")
    return "\n".join(L)


def _system_stats_block() -> str:
    """Compact HTML block reporting disk / RAM / GPU plus warnings (low disk,
    hot GPU). Always emitted at the bottom of every digest so the user
    notices infra issues without opening the UI."""
    s, warns = _system_snapshot()
    if not s and not warns:
        return ""
    cards = []
    ram = s.get("ram") or {}
    disk = s.get("disk") or {}
    if s.get("cpu_percent") is not None:
        cards.append(("CPU", f"{int(s['cpu_percent'])}%"))
    if ram.get("total_gb"):
        cards.append(("RAM",
                      f"{ram.get('used_gb','?')} / {ram['total_gb']} GB"))
    if disk.get("total_gb"):
        cards.append(("Disk free", f"{disk.get('free_gb','?')} GB"))
        cards.append(("Disk used", f"{int(disk.get('percent') or 0)}%"))
    gpus = s.get("gpus") or []
    if gpus:
        avg_util = int(sum(g.get("util_pct") or 0 for g in gpus) / len(gpus))
        max_t = max((int(g.get("temp_c") or 0) for g in gpus), default=0)
        cards.append((f"GPUs ({len(gpus)})", f"{avg_util}% · {max_t}°C"))
    block = (
        '<div style="font-size:11px;color:#6366F1;font-weight:700;'
        'text-transform:uppercase;letter-spacing:.5px;margin:22px 0 5px;">'
        'Node health</div>'
        + _stat_cards(cards))
    if warns:
        items = ""
        for w in warns:
            col = _SEV_COLOR.get(w["severity"], "#9BA1A8")
            items += (
                f'<tr><td style="padding:9px 12px;border-left:3px solid {col};'
                f'background:#1A1D22;border-radius:5px;">'
                f'<span style="color:{col};font-weight:700;font-size:11px;'
                f'text-transform:uppercase;letter-spacing:.4px;'
                f'margin-right:8px;">'
                f'{_esc(w["severity"])}</span>'
                f'<span style="color:#E6E8EB;font-size:12.5px;">'
                f'{_esc(w["msg"])}</span></td></tr>'
                f'<tr><td style="height:6px;"></td></tr>')
        block += (
            f'<table role="presentation" style="width:100%;'
            f'border-collapse:collapse;margin-top:8px;">{items}</table>')
    return block


# ─────────────────────────── paper-mode digest ─────────────────────────────

_EMAIL_STATE_KEY = "email_state_paper"


def _email_state_get() -> dict:
    """Last-send snapshot used to compute deltas in the paper digest."""
    db = SessionLocal()
    try:
        row = db.query(Setting).filter(
            Setting.key == _EMAIL_STATE_KEY).first()
        if row and isinstance(row.value, dict):
            return dict(row.value)
        return {}
    finally:
        db.close()


def _email_state_set(snap: dict) -> None:
    db = SessionLocal()
    try:
        row = db.query(Setting).filter(
            Setting.key == _EMAIL_STATE_KEY).first()
        if row:
            row.value = snap
        else:
            db.add(Setting(key=_EMAIL_STATE_KEY, value=snap))
        db.commit()
    finally:
        db.close()


def _paper_snapshot(db) -> dict:
    """Mini snapshot used for since-last-email diffs."""
    from .models import (PaperClaim, PaperDecision, PaperVersion, Run)
    claim_ids = sorted(c.id for c in db.query(PaperClaim).all())
    return {
        "at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "claim_ids": claim_ids,
        "decision_ids_pending": sorted(
            d.id for d in db.query(PaperDecision).filter(
                PaperDecision.status == "pending").all()),
        "decision_ids_resolved": sorted(
            d.id for d in db.query(PaperDecision).filter(
                PaperDecision.status.in_(("approved", "rejected"))).all()),
        "paper_run_ids_done": sorted(
            r.id for r in db.query(Run).filter(
                Run.context == "paper",
                Run.status.in_(("kept", "success", "done",
                                "crashed", "failed"))).all()),
        "version_ids": sorted(v.id for v in db.query(PaperVersion).all()),
    }


def _share_url(cfg: dict) -> str | None:
    """If the user has minted a public share link, surface it in the email
    so they can forward to co-authors."""
    db = SessionLocal()
    try:
        row = db.query(Setting).filter(
            Setting.key == "paper_share_token").first()
    finally:
        db.close()
    if not row or not isinstance(row.value, dict):
        return None
    tok = row.value.get("token")
    if not tok:
        return None
    base = _dashboard_url(cfg)
    return f"{base}/p/{tok}" if base else f"/p/{tok}"


def _paper_digest_email(window_hours: float):
    """Compose the paper-mode (subject, text, html, images). Structured
    around what a paper author actually needs daily: headline progress,
    what's new since last email, decisions waiting on them, what the
    author agent did today, top results to integrate, PI nudges, draft
    section health, blockers, and a share link for co-authors."""
    from .models import (PaperClaim, PaperDecision, PaperFigure, PaperMeta,
                         PaperSection, PaperVersion, Project, Run)
    from . import paper as _paper

    cfg = _cfg()
    prev = _email_state_get()
    db = SessionLocal()
    try:
        proj = db.query(Project).first()
        if not proj:
            return None, None, None, None
        meta = db.query(PaperMeta).first()
        pname = proj.name
        days = _paper.days_till_deadline()
        claims = db.query(PaperClaim).order_by(PaperClaim.idx).all()
        n_claims = len(claims)
        n_ready = sum(1 for c in claims if c.ready)
        n_killed = sum(1 for c in claims if c.status == "killed")
        decisions_pending = db.query(PaperDecision).filter(
            PaperDecision.status == "pending").order_by(
            PaperDecision.priority.desc(),
            PaperDecision.created_at.asc()).all()
        paper_runs = db.query(Run).filter(Run.context == "paper").all()
        running = [r for r in paper_runs if r.status == "running"]
        finished_recent = [r for r in paper_runs
                            if r.status in ("kept", "success", "done",
                                            "crashed", "failed")]
        sections = db.query(PaperSection).order_by(PaperSection.slug).all()
        versions = db.query(PaperVersion).order_by(
            PaperVersion.created_at.desc()).limit(3).all()
        snap = _paper_snapshot(db)
    finally:
        db.close()

    # Compute deltas since last send
    prev_claims = set(prev.get("claim_ids") or [])
    new_claims = [c for c in claims if c.id not in prev_claims]
    prev_runs_done = set(prev.get("paper_run_ids_done") or [])
    new_runs_done = [r for r in finished_recent
                      if r.id not in prev_runs_done]
    prev_dec_pending = set(prev.get("decision_ids_pending") or [])
    prev_dec_resolved = set(prev.get("decision_ids_resolved") or [])
    snap_resolved = set(snap["decision_ids_resolved"])
    snap_pending = set(snap["decision_ids_pending"])
    newly_resolved = snap_resolved - prev_dec_resolved
    newly_filed = snap_pending - prev_dec_pending
    n_new_versions = len(set(snap["version_ids"])
                         - set(prev.get("version_ids") or []))

    # Top results: completed paper_runs in this window, best-metric first
    metric = proj.validation_metric or "metric"
    maximize = proj.metric_direction == "maximize"
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(
        hours=window_hours)
    in_window = [r for r in finished_recent
                  if (_parse_iso(r.ended_at) or dt.datetime.min.replace(
                      tzinfo=dt.timezone.utc)) >= cutoff]

    def _key(r):
        return r.headline_metric if r.headline_metric is not None \
            else (-1e18 if maximize else 1e18)
    top_runs = sorted(
        [r for r in in_window if r.headline_metric is not None],
        key=_key, reverse=maximize)[:5]
    crashed = [r for r in in_window if r.status in ("crashed", "failed")]

    # Section health roll-up
    sec_status = {}
    for s in sections:
        sec_status[s.status] = sec_status.get(s.status, 0) + 1

    # Author agent today: last N commits
    folder = _paper.paper_folder()
    commits = _paper.list_commits(folder, limit=8) if folder else []

    # ─────────────── HTML body ─────────────────────────────────────────
    deadline_str = (f" · <b style=\"color:#F59E0B\">{days:.1f} days "
                    "to deadline</b>" if days is not None else "")
    headline = (
        f'<p style="margin:0 0 4px;">Paper status for '
        f'<b style="color:#E6E8EB;">{_esc(pname)}</b>'
        f'{deadline_str}</p>'
        f'<p style="margin:0 0 12px;font-size:13px;color:#9BA1A8;">'
        f'{n_claims} claims · {n_ready} ready · '
        f'{len(decisions_pending)} decision'
        f'{"" if len(decisions_pending) == 1 else "s"} waiting'
        f' · {len(running)} run{"" if len(running) == 1 else "s"} '
        f'in flight</p>')
    cards = [
        ("claims", str(n_claims)),
        ("ready", str(n_ready)),
        ("waiting", str(len(decisions_pending))),
        ("running", str(len(running))),
        ("done {}h".format(int(window_hours)), str(len(in_window))),
    ]
    if days is not None:
        cards.append(("deadline", f"{days:.1f}d"))

    body = headline + _stat_cards(cards)

    # What's new since last email
    if prev.get("at"):
        whatsnew = []
        if new_claims:
            whatsnew.append(f"+{len(new_claims)} new claim"
                            f"{'s' if len(new_claims) != 1 else ''}: "
                            + ", ".join(c.title[:60] for c in new_claims[:3]))
        if new_runs_done:
            whatsnew.append(f"+{len(new_runs_done)} ablation"
                            f"{'s' if len(new_runs_done) != 1 else ''} "
                            "completed")
        if newly_resolved:
            whatsnew.append(f"{len(newly_resolved)} decision"
                            f"{'s' if len(newly_resolved) != 1 else ''} "
                            "resolved")
        if newly_filed:
            whatsnew.append(f"{len(newly_filed)} new decision"
                            f"{'s' if len(newly_filed) != 1 else ''} filed")
        if n_new_versions:
            whatsnew.append(f"{n_new_versions} new paper version"
                            f"{'s' if n_new_versions != 1 else ''} pinned")
        body += _section("Since your last email", whatsnew or [
            "No new claims, runs, or decisions since the last digest."])
    else:
        body += _section("Welcome to paper mode", [
            "This is your first paper-mode digest. From here you'll get one "
            "summary per day with what's waiting on you."])

    # Decisions waiting — the most important part of the email
    dec_rows = []
    for d in decisions_pending[:8]:
        kind = (d.kind or "?").replace("_", " ")
        title = (d.title or d.body_md or kind)[:90]
        prio = "★" * min(int(d.priority or 0), 3) or "·"
        dec_rows.append(f"{prio} [{kind}]  {title}")
    if len(decisions_pending) > 8:
        dec_rows.append(f"… and {len(decisions_pending) - 8} more in the "
                        "Decision Queue")
    body += _section(
        f"⏵ Decisions waiting on you ({len(decisions_pending)})",
        dec_rows or ["Inbox zero — no decisions queued."])

    # Top results in window
    if top_runs or crashed:
        run_lines = []
        for r in top_runs:
            hm = (f"{r.headline_metric:.4f}"
                   if r.headline_metric is not None else "—")
            cfgr = r.config if isinstance(r.config, dict) else {}
            tag = (cfgr.get("what") or r.paper_role or "ablation")[:60]
            run_lines.append(
                f"{(r.run_name or r.id)[:32]} → {metric}={hm}  ({tag})")
        if crashed:
            run_lines.append(
                f"⚠ {len(crashed)} crashed / failed — see Decision Queue")
        body += _section(f"Top results in the last {window_hours:g}h",
                         run_lines)

    # Author agent's day: commits
    if commits:
        c_lines = []
        for c in commits[:6]:
            msg = (c.get("subject") or c.get("message") or "")[:80]
            when = (c.get("at") or c.get("date") or "")[:16]
            c_lines.append(f"{msg}  ({when})")
        body += _section("Author agent · paper commits", c_lines)

    # Section health
    if sec_status:
        sec_lines = [f"{k}: {v}" for k, v in sec_status.items()]
        body += _section("Draft section health", sec_lines)

    # Blockers
    blockers = []
    for s in sections:
        if s.status == "blocked":
            why = (f"waiting on claim {s.blocked_on_claim_id}"
                   if s.blocked_on_claim_id
                   else (f"waiting on run {s.blocked_on_run_id}"
                         if s.blocked_on_run_id else "blocked"))
            blockers.append(f"§ {s.title or s.slug}  —  {why}")
    if crashed:
        for r in crashed[:3]:
            blockers.append(f"run {r.run_name or r.id} crashed — "
                            "needs author triage")
    if blockers:
        body += _section("Blockers", blockers)

    # Versions section (most recent pinned snapshot)
    if versions:
        v_lines = [f"{v.label or v.id}  ({v.created_at[:10]})"
                   for v in versions]
        body += _section("Recently pinned versions", v_lines)

    # Share link for co-authors — always make this prominent if it exists
    share = _share_url(cfg)
    if share:
        body += (
            f'<a href="{_esc(share)}" style="display:block;text-align:center;'
            'background:#A78BFA;color:#0B0D10;text-decoration:none;'
            'font-weight:700;font-size:13px;padding:11px;border-radius:9px;'
            'margin:14px 0 4px;">'
            'Forward read-only paper to a co-author &rarr;'
            f'</a>'
            '<p style="margin:0 0 8px;font-size:11px;color:#5C636B;'
            'text-align:center;">'
            'No login required for collaborators.</p>')

    # Append the universal system-stats / warnings block
    body += _system_stats_block()

    # ─────────────── text/plain ────────────────────────────────────────
    T = [
        f"autoresearcherUI paper digest - {pname}",
        f"Window: last {window_hours:g}h  ("
        f"{dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%d %H:%M UTC')})",
    ]
    if days is not None:
        T.append(f"Deadline: {days:.1f} days away  "
                  f"({(meta.venue if meta else '?')})")
    T += ["",
          f"Claims:    {n_claims}   (ready: {n_ready}, killed: {n_killed})",
          f"Decisions: {len(decisions_pending)} waiting on you",
          f"Runs:      {len(running)} in flight, "
          f"{len(in_window)} completed in window",
          ""]
    if prev.get("at"):
        T.append("== Since your last email ==")
        if new_claims:
            T.append(f"  +{len(new_claims)} new claim(s)")
        if new_runs_done:
            T.append(f"  +{len(new_runs_done)} ablation(s) completed")
        if newly_resolved:
            T.append(f"  {len(newly_resolved)} decision(s) resolved")
        if newly_filed:
            T.append(f"  {len(newly_filed)} new decision(s) filed")
        if n_new_versions:
            T.append(f"  {n_new_versions} new version(s) pinned")
        if not any([new_claims, new_runs_done, newly_resolved,
                    newly_filed, n_new_versions]):
            T.append("  (no changes since last digest)")
        T.append("")
    if decisions_pending:
        T.append(f"== Decisions waiting ({len(decisions_pending)}) ==")
        for d in decisions_pending[:8]:
            kind = (d.kind or "?").replace("_", " ")
            T.append(f"  [{kind:14}] {(d.title or '')[:80]}")
        T.append("")
    if top_runs:
        T.append(f"== Top results in {window_hours:g}h ==")
        for r in top_runs:
            hm = (f"{r.headline_metric:.4f}"
                   if r.headline_metric is not None else "—")
            T.append(f"  {(r.run_name or r.id)[:24]:24} {metric}={hm}")
        T.append("")
    if crashed:
        T.append(f"== Crashed / failed ({len(crashed)}) ==")
        for r in crashed[:5]:
            T.append(f"  {(r.run_name or r.id)[:24]}")
        T.append("")
    if blockers:
        T.append("== Blockers ==")
        for b in blockers[:6]:
            T.append(f"  {b}")
        T.append("")
    if share:
        T += ["", f"Co-author share link: {share}", ""]
    T += ["", "- autoresearcherUI"]
    text = "\n".join(T) + "\n\n" + _system_stats_text()

    subject = (f"[{pname}] paper digest — "
               f"{len(decisions_pending)} decision"
               f"{'' if len(decisions_pending) == 1 else 's'} waiting, "
               f"{len(in_window)} run"
               f"{'' if len(in_window) == 1 else 's'} done"
               + (f", {days:.1f}d to deadline" if days is not None else ""))

    # Update snapshot so next email's deltas are accurate
    try:
        _email_state_set(snap)
    except Exception as e:                              # noqa: BLE001
        print(f"[notify] failed to persist email_state: {e}", flush=True)

    html = _shell(f"Paper digest — {pname}", body, _dashboard_url(cfg))
    return subject, text, html, {}


# ───────────────────────────── scheduler ───────────────────────────────────

_started = False
_lock = threading.Lock()


def start_scheduler() -> None:
    """Start the once-per-process background digest scheduler."""
    global _started
    with _lock:
        if _started:
            return
        _started = True
    threading.Thread(target=_loop, daemon=True,
                     name="notify-scheduler").start()
    print("[notify] digest scheduler started", flush=True)


def _loop() -> None:
    last_sent = time.time()        # anchor: never fire instantly on boot
    last_cad: str | None = None
    while True:
        time.sleep(60)
        try:
            cfg = _cfg()
            cad = _cadence(cfg)
            if cad != last_cad:                       # cadence changed
                last_cad = cad
                last_sent = time.time()              # restart the window
            hrs = _cadence_hours(cad)
            if hrs is None:                           # off / immediate
                continue
            if time.time() - last_sent >= hrs * 3600:
                # Cheap pre-check so a paused user doesn't pay the
                # cost of building the digest's HTML + charts every
                # cadence tick. send() also gates on emails_paused
                # as the source of truth.
                if not emails_paused():
                    subject, text, html, images = digest_email(hrs)
                    if subject:
                        send(subject, text, html, images)
                last_sent = time.time()
        except Exception as e:                       # noqa: BLE001
            print(f"[notify] scheduler error: {e}", flush=True)
