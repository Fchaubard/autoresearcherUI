# Context when agent settings change

Research and author settings are saved immediately. Their running processes
change only when research is not paused or halted and the selected pane has no
active experiment or queue children. Changing credentials or local-server
settings restarts only roles using that provider. Managed Flint waits until its
selected local model is healthy; the current agent stays alive while it loads.

For OpenAI-to-OpenAI model or reasoning-effort changes, the app resumes the
existing Codex conversation by its exact session UUID. It verifies the live
pane's process identity, workspace, open transcript, private Codex home, source
model and app-owned OpenAI provider against Codex's thread database before
stopping the old process, and checks them again inside the restart lock. The
resume command pins the requested target model, effort and OpenAI endpoint.
It never selects a global latest conversation or substitutes another model.
If the conversation cannot be verified, the current agent stays running and
the saved change remains pending with `conversation_unavailable`.
The exact verified resume intent is committed before stopping the old process.
If the replacement fails, recovery validates and retries that same conversation;
it cannot silently start a fresh one. The intent is cleared only after a new
process is observed owning the expected transcript.

Codex resumes saved conversation history, including its normal compaction.
This does not preserve process memory: tool variables, `functions.store/load`
values and other transient state must be rebuilt from project files. The
resume message states that limitation explicitly. Experiments running outside
the agent pane continue using their existing processes and checkpoints.

Other provider changes and other configuration changes start a fresh CLI
conversation. Before restarting, the app attempts to save `ARUI_HANDOFF.md`
with source/target selections, bounded recent visible Codex messages when
their ownership can be verified, the latest saved research status, dashboard
messages, active run IDs and links to durable project files. It excludes
hidden reasoning, tool output, environment values and raw run configuration,
and redacts configured credentials. This is a summary, not a native transcript
import. Native continuation of Claude, Gemini and OpenCode conversations is
not currently implemented.
The handoff is written atomically with mode 0600 and excluded through the
repository's local `info/exclude`, including linked worktrees; it is not committed.

Settings responses report `context_preservation` separately for each applied
role: `native_conversation`, `workspace_handoff`, or `workspace_state_only`.
The last value means a safe recent excerpt or handoff could not be recovered;
only existing project files and the run ledger are available. A successful
restart still depends on the provider CLI starting successfully.

Tests verify source/target separation, exact session ownership, process-ID
reuse rejection, provider-specific settings, pause/training gates, handoff
redaction, and missing-context deferral. An optional integration test runs the
installed Codex CLI with a private home and private tmux server, resumes an
explicit synthetic transcript, and checks that earlier messages are visible.
Its inference endpoint is unreachable loopback, so it makes no paid model
calls and does not touch a live project.
