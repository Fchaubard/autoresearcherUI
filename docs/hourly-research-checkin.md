# Hourly research check-in

`bin/arui_research_checkin.py` collects bounded local evidence and sends one
operator-requested reminder to the existing research conversation. The configured
PI still performs its separate model-based review. The script itself makes no
model calls, sends no email, and never controls experiment or service processes.

The researcher defaults, GPU-idle reminder and PI prompt prioritize the current
operator purpose and budget. Idle capacity should trigger a valid ready
experiment or work on its blocker, not obsolete jobs or arbitrary sweeps.
Negative evidence must be preserved; it is not permission to label a goal met
or a requirement to spend indefinitely until a positive result appears.

Enable `research_oversight_enabled` in onboarding settings and place the current
operator contract in the research workspace as `OPERATOR_RESEARCH_BRIEF.md`.
Optional `research_campaign_started_at` (timezone-aware ISO timestamp) and
`research_campaign_gpu_hour_cap` add cumulative budget reporting. Charged GPU time
uses registered duration times declared GPU allocation, including setup; it is
not a measurement of active GPU compute. The agent receives the budget evidence;
this reminder does not itself impose a process-level spending limit.

Run once with `--dry-run` to inspect the evidence without sending a message:

```sh
python3 bin/arui_research_checkin.py \
  --db /var/lib/autoresearcherui/data/autoresearch.db \
  --workspace /path/to/research \
  --out /var/lib/autoresearcherui/data/research_oversight \
  --dry-run
```

For live delivery, omit `--dry-run`. Schedule that command with the system cron
once an hour; preserve other crontab entries. The process lock and 55-minute
cooldown prevent overlapping/duplicate sends while tolerating normal cron
jitter. An ambiguous timeout is recorded and not retried immediately. Verify
`latest_check.json` for acceptance; `delivery: queued` means the native CLI
accepted the reminder, not that the research assessment has completed.

The check respects disabled oversight, paused/halted research and open HALT
directives, and rechecks pause after collecting evidence. Stop future checks by
disabling `research_oversight_enabled` or removing its cron entry. Results are
stored privately in the output directory; the normal agent-message API also
makes the reminder visible in the dashboard. No credentials enter snapshots or
delivery receipts. The server and research agent must remain available.
