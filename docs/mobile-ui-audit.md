# iPhone and control audit — 2026-10-27

The original email icon overlaid the mobile conversation Send button. The
launcher now has a dedicated bottom row outside page content, and disappears
while the conversation is open. The panel has its own close button. Safe-area
padding and visual-viewport sizing keep the composer and dialogs in the visible
area when the available viewport changes.

The post-submit research review now presents three mobile panels: Research
plan, Review status, and Discuss. The editable plan and confirmation remain
in the Research plan panel; each panel scrolls independently. Desktop retains
its three-column layout.

Other fixes found during the audit:

- Analysis and Files switch to one column on phones; tables and charts scroll
  inside their own containers. Analysis menus are clamped inside the viewport.
- Settings, halt reports, proposals, token errors, approval dialogs, and all
  other modal families fit the viewport and scroll to their action buttons.
- Paper tab content no longer collapses below the PDF viewer. Toolbars wrap,
  tabs scroll horizontally, and wide evidence tables remain usable.
- Editable controls use 16px text to avoid Safari focus zoom; buttons have 44px
  targets, with a compact horizontally scrolling quick-message row.
- Enter on a focused Cancel button cancels the dialog. Previously the global
  key handler confirmed destructive actions even when Cancel had focus.
- Halt reports distinguish verified success from partial failure, show remaining
  workers/errors, support Retry halt, and let the user keep research halted.
  Dismissing a stop report never sends a resume request.
- Failed agent or scoping chat requests preserve the unsent draft.
- Claim readiness stars now save their state. Citation comparison notes have an
  explicit Save notes action, error feedback, and drafts that survive tab
  navigation after failed saves.
- New File refuses to overwrite an existing path. Saving a file preserves
  newer edits and the dirty state of another tab opened during the request.
- Analysis saves report errors and offer Retry; empty layouts persist, and
  Reset explicitly restores defaults only after the server accepts it.
- Empty filters do not hide all runs; Clear all removes Hide crashed too, and
  date comparisons use dates. Clipboard and share-revoke errors retain usable
  controls instead of reporting success.
- Paper scaffold, citation approval, reviewer simulation, and version diff
  failures remain actionable. A single pinned draft can be compared with HEAD.
- Returning to research reports partial agent-restart failure while updating
  the mode correctly. Independent paper experiments are reported as continuing
  to completion; a paused project stays paused.

## Coverage inventory

| Surface | Browser checks |
|---|---|
| Dashboard | Progress chart, status, six stats, table, filters, search, halt launcher, conversation dock |
| Navigation | All seven menu routes, Settings, Archive, Reset cancellation |
| Conversation | Four tabs, Send POST, close/reopen, shrinking viewport, failed send retains draft |
| Analysis | Run list, populated panel, filter dialog, date comparisons, clear, add/edit panel dialogs, sort/bulk menus, selection/clear; save retry, explicit reset, empty layout persistence |
| Files | File list, filter, refresh/new/save layout, editor pane sizing; duplicate-file refusal, save with tab switching and newer edits |
| Lessons | Populated lesson with long text |
| Paper | Research-mode entry; Today, Claim Coverage, Paper Plan, Critical Path, Related Work, Versions, Share; populated claims, runs, citations, versions; readiness and citation-save success/error paths; scaffold/approval/reviewer errors, HEAD diff, share-revoke errors, return-to-research success/partial/error |
| System | GPU/host sections and both cleanup preview dialogs |
| SSH keys | Key list, public key, add/delete/copy controls fit; denied clipboard permission |
| Onboarding | Long form, submit target, readable inputs, token results |
| Scoping | Plan/review/discussion tabs, discussion POST, edited confirmation POST, boot overlay |
| Access | Login form and unlock target |
| Settings | Long form and bottom Save action; provider/settings behavior covered separately in settings.spec.js |
| Halt | Verified stop, partial HTTP failure, remaining-worker report, retry, resume disabled on incomplete stop, dismiss preserves pause |
| Generic dialogs | Long alert, prompt, confirmation, keyboard Cancel, focus handling |

Sixteen additional dialog families are opened with fixture data and every
enabled button is checked for an unobstructed hit target: research health,
novelty, council review, archive, paper setup, paper revert, multi-reviewer paper
proposal, token errors, version diff, submission helper, old-log purge, SOTA
purge, analysis filter, add panel, edit panel, and prompt. Operator approval is
also tested with a 20-row ablation table.

The final complete matrix passed 132 tests across WebKit and Chromium. It
includes the responsive matrix, settings/model controls, the interaction
regressions above, and desktop layout checks. JavaScript syntax and Git
whitespace checks also passed.

## Reproduction and evidence

See [browser test instructions](../tests/browser/README.md). APIs are mocked;
these checks verify rendered layout and browser behavior. Python backend tests
are responsible for actual settings persistence, process termination, model
providers, and other server side effects.

Representative fixture screenshots are committed under `docs/screenshots/`:

- [Conversation](screenshots/mobile-conversation.png)
- [Research plan](screenshots/mobile-scope-plan.png)
- [Settings actions](screenshots/mobile-settings-save.png)
- [Halt report](screenshots/mobile-halt.png)
- [Page/dialog contact sheet](screenshots/mobile-audit-contact.jpg)

WebKit and Chromium were exercised at 320, 375, 390, and 430px portrait widths,
844×390 landscape, and 1440×1000 desktop. Viewport resizing is tested, but this
is not a physical iPhone test and does not emulate every native keyboard,
notch, browser-toolbar, or file-picker interaction. Monaco's remote editor
loading is blocked by the isolated harness; the Files layout and save concurrency
with an editor-model fixture are covered, while the real editor and GPU
execution are outside this browser fixture suite.
