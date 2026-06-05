"""Default watchdog scripts.

Each script is one Python module exposing:

  DEFAULT_PARAMS: dict[str, Any]
      Tunable params with sensible ML-research defaults. The agent
      reviews these at onboarding (PR 5) and may override per-project.

  DEFAULT_ENABLED: bool = True
      Whether the script ships ON by default.

  KILLS_RUN: bool = False
      Whether firing this script should automatically kill the run.

  describe() -> str
      Human-readable one-liner shown in the onboarding modal.

  check(run, metrics, params) -> Issue | None
      Pure-ish function: read the run + its metrics + the merged params
      and return an Issue if the condition fires, or None.

  on_fire(run, issue, params) -> dict (optional)
      Returns ``{kill_run, page_agent, page_message}`` to override the
      runner's default action. If absent, the runner uses
      ``{kill_run: KILLS_RUN, page_agent: True}`` and a generic page
      message.
"""
