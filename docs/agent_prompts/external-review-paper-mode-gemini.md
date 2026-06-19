# Paper Mode review — Gemini 2.5 Pro

Alright, let's get into it. This is a strong v1 spec. The core loop—assess, plan, execute, write—is sound. You've correctly identified the major user moments. My feedback is focused on hardening this against the messy reality of research. You've asked for directness, so I'll skip the pleasantries.

### 1. WORKFLOW GAPS

The biggest gap is the spec's assumption that research is a two-stage, linear process: `explore -> prove`. In reality, the "proving" stage (running ablations, baselines) often uncovers fundamental flaws or new research directions. The model is too rigid.

1.  **The "Research Off" Switch is Dangerous.** The spec says "the agent stops exploring" and the PI/strategic council agents are turned off. This is a mistake. The most insightful moments in the 11th hour of paper-writing come from a surprising ablation result that kicks off a new mini-exploration. Forcing a full mode-revert to follow that thread is too heavyweight. It treats a paper as a reporting exercise, when it's often the final, most intense phase of research itself.
2.  **The Author Agent as a Bottleneck.** The spec states the Author Agent "maintains `paper_runs.md`". This implies the user must *persuade* the agent via chat to add a new baseline or a "what if" experiment. This will be infuriating. Researchers need direct, low-latency control over the experiment queue. An agent is a great *planner*, but a terrible *gatekeeper*.
3.  **The Gantt Chart is a Lie.** I've never seen a research project's experiment schedule hold for more than a day. A single run diverges and takes 3 days instead of 12 hours, a key baseline is harder to implement than expected, a result from run A obviates the need for runs B, C, and D. A Gantt chart gives a false sense of deterministic progress. It's a tool for factory floors, not research labs. It will be perpetually wrong and the user will learn to ignore it.
4.  **The "2-3 Claims" Heuristic is Too Constraining.** Some papers have one massive claim. Some have five smaller, interlocking ones. Hard-coding "2-3 claims" in the agent's prompt will lead to it force-fitting the research into an unnatural structure.

#### CONCRETE FIXES:

1.  **Soften the Mode Switch.** Don't turn off the research/PI agents. Instead, change their *objective*. In "Paper Mode," they are re-tasked to support the paper's claims. The user should be able to queue both "paper runs" (for a specific figure) and "exploratory runs" (e.g., "dig into why the frobnicator ablation failed"). Differentiate them in the UI with a tag or icon. The core research engine should *always* be running.
2.  **User-Owned Run Queue.** The Author Agent *proposes* the initial `paper_runs.md`. The user must have a first-class UI to add, remove, reorder, and edit runs in that plan directly. The agent can *comment* on the plan ("Warning: you removed the main baseline for Claim 1"), but the user has final say. The UI should be as direct as editing a spreadsheet.
3.  **Replace the Gantt with a Dependency Graph.** Instead of a timeline, show a DAG. `Figure 3` depends on `Run 3.1`, `Run 3.2`, and `Run 3.3`. `Run 3.2` depends on the checkpoint from `Run 1.1`. This correctly models the dependencies and uncertainties. The UI can then show what's `runnable`, `blocked`, and `done`. For estimation, provide a simple, aggregate ETA ("~4 days of GPU time remaining"), not a precise Gantt.
4.  **Generalize the Agent's Claim Prompt.** Change the prompt from "Decide the strongest 2-3 claims" to "Identify the strongest, most defensible claims this research can support. Articulate each one clearly in `claims.md`." Let the agent decide the number.

### 2. UX / VISIBILITY ISSUES

1.  **Read-only LaTeX is a Non-Starter.** I cannot stress this enough. No serious researcher will tolerate a workflow where they can't directly edit the text of their paper. Having to ask an agent to rephrase a sentence or fix a typo is a complete workflow-killer. This moves the feature from "helpful assistant" to "micromanaging boss."
2.  **PDF Compilation Failures.** `latexmk` fails. Often. A missing `\}` bracket, a bad bibtex entry, a figure path typo. The spec's "spinner overlay" is insufficient. What happens when it fails? The user will be stuck with a stale PDF and no idea why.
3.  **The Council Pre-Flip Modal is Too Synchronous.** The user decides to write, clicks the button, and is now blocked, waiting for three different LLM APIs to respond. This is a momentum-killer.

#### CONCRETE FIXES:

1.  **Enable Direct Editing.** This is a hard problem, but it's table stakes. The simplest v1 is a plain text editor (e.g., CodeMirror) in the "LaTeX" view. When the user edits, you save the file. When the *agent* wants to edit, it should do so via a git commit. The UI can then show a "The agent has updated this file. [View diff] [Reload]". This creates a clear, auditable history and avoids complex real-time merging.
2.  **Surface LaTeX Logs.** When a PDF compilation is triggered, the right rail should show a live view of the `latexmk` output log. If it fails, the log stays there, with errors highlighted, so the user can debug.
3.  **Make the Pre-Flip Asynchronous.** When the user clicks `Paper`, immediately show a transient state: "Assessing readiness... The council is reviewing your work. This may take a few minutes. We'll notify you when the report is ready." The analysis becomes a persistent artifact, like a "Paper Proposal," that the user can review and act on when it's complete.

### 3. ARCHITECTURAL GAPS

1.  **Markdown as a Database.** Using `paper_runs.md` and `paper_figures.md` as the source of truth for the UI is brittle. The backend will be constantly parsing these files. If the agent messes up the markdown table syntax, the UI breaks. This is a classic "stringly-typed" architecture problem.
2.  **Author Agent's Contract is Vague.** The spec says the agent "maintains" and "updates" files. This needs to be rigorously defined. Does it overwrite the whole file? Does it use `sed`-like commands? How does it avoid clobbering user edits or comments? Without a clear contract, the agent will be unpredictable.
3.  **Data Siloing in the Reversal Flow.** When reverting to Research mode, the spec says the `paper/` folder is kept. But what about the *structured data* from the completed paper runs? Those metrics, checkpoints, and logs are invaluable research artifacts. They shouldn't be left in a paused side-project; they should be folded back into the main experiment database so the research agent can learn from them.

#### CONCRETE FIXES:

1.  **Use Markdown for Agent I/O, Database for UI.** The markdown files are the agent's "API." A backend service should watch these files, parse them, and sync their state into proper database tables (`paper_runs`, `paper_figures`, etc.). The UI reads from these stable, structured tables. The agent writes text, the system normalizes it.
2.  **Git as the Agent's Contract.** The Author Agent's workspace (`paper/`) should be a git repository. Every change the agent makes must be a commit. The commit message should be the "spinner verb" (e.g., "Regenerate Figure 3 with run-xyz results"). This gives you diffs, history, and a clear mechanism for handling user edits (they're just commits on the same branch).
3.  **Unify the Run Database.** There should be one `runs` table. Paper runs are just regular runs with a `context='paper'` flag and a foreign key to `paper_figures.id`. When you revert, these runs are still there, visible in the main dashboard, tagged as "From Paper Attempt #1". The research agent can then query them and learn why the paper attempt failed.

### 4. SIMPLER ALTERNATIVES

1.  **The Auto-Revert Watcher is Over-engineered.** A background cron job with a hard-coded 40% failure threshold is complex and arbitrary. The user will likely find it noisy.
2.  **The Council Pre-Flip is Heavy.** A full round-robin with 3+ models is great for rigor but slow.

#### CONCRETE FIXES:

1.  **Delegate Reversal Suggestions to the Author Agent.** Make this part of the agent's core prompt. "You are ruthless about killing claims the data does not support. If you observe a pattern of failing ablations, your primary job is to report this in the Summary and recommend a reversion to Research Mode." This is more organic, contextual, and requires no new background services.
2.  **Single-Model Pre-Flip.** For v1, just use your best/primary council model for the pre-flip assessment. It's faster and cheaper. You can add the multi-model "second opinion" feature later if users feel the advice is biased.

### 5. THE HARD PROBLEMS

Answering my top 4:

1.  **Should "paper mode" be one-way?** Absolutely not. The spec is correct to allow round-trips. Research is iterative. Forcing users to "abandon" a paper to explore a new lead is user-hostile and misunderstands the process. Your design is good, but my architectural fixes above (unifying the run DB) will make the round-trip more seamless and productive.
2.  **Author Agent ↔ research agent: same session?** Different sessions. Absolutely critical. They have different ontologies, different goals, and different prompts. The research agent is a divergent, creative explorer. The author agent is a convergent, structured synthesizer and project manager. Merging them will produce a confused agent that is bad at both jobs. The extra cost is justified by the massive increase in task-specific performance.
3.  **Latex viewer: read-only or editable?** Must be editable. As I said above, this is a deal-breaker. Start with a simple text editor that saves to the file system. Use git underneath to version changes from the user and the agent. Don't even attempt real-time collaborative editing in v1.
4.  **PDF rendering: server-side vs. client-side?** Server-side `latexmk` is the only way to get a canonical, correct PDF. The dependency is a deployment problem (solve with Docker), not a product one. However, the idea of using client-side rendering for a *fast preview* is brilliant.
    **Recommendation:** The "LaTeX" view should use a JS library (like MathJax/KaTeX) to render a fast but imperfect HTML preview. The "PDF" view shows the `<iframe>` of the true PDF from the server. This gives the user both speed and accuracy, letting them choose which they need at any moment.

### 6. TL;DR — TOP 3 CHANGES

1.  **Abandon the Strict Dichotomy.** Don't turn off the research agent in Paper Mode. Re-task it to support the paper. Allow users to queue both "paper" and "exploratory" runs at all times. The two modes are different views/contexts on top of a single, continuous research process.
2.  **Give the User Direct Control.** The user, not the agent, must be the master of the experiment plan (`paper_runs`). The agent generates the first draft of the plan, but the user needs a simple, direct UI to add, edit, and re-prioritize runs without asking permission.
3.  **Replace the Gantt Chart with a Dependency Graph.** The Gantt's deterministic timeline is a fiction that will frustrate users. A dependency graph is a more honest and useful representation of a research plan, showing what's blocked and what's runnable now.