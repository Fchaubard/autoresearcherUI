# Mobile agent messages and live updates

The operator reported two separate failures: the terminal stopped updating until
a page refresh, and app Send wrote a message into the native agent's input but
left Enter unaccepted.

The terminal freeze reproduced with the real bundled xterm in WebKit at an
iPhone-sized viewport. Starting at 390×844, the terminal followed its live buffer
at viewport/base row 79. Shrinking to 390×360 for the keyboard changed those rows
to 101/105. The old scroll lock interpreted the resize as deliberately reading
history, held every later frame, and stayed frozen after restoring the viewport.
The server continued delivering updates 8→18→28 while only update 8 remained
visible. With the fix, visible updates advance 8→18→28 and viewport/base rows
stay equal. Deliberately scrolling up still preserves the reading position.

The live view now bounds requests, aborts disposed streams, and refreshes after
phone resume. Plain-text fallback also updates if xterm cannot load. Chat
recovery fetches recent saved messages after reconnect and periodically while
visible, deduplicates server echoes, and restores chronological order without
moving an operator who is reading older activity.
Terminal fits are coalesced into animation frames instead of mutating layout
inside ResizeObserver delivery; closing the view cancels any pending fit.

The final production visual check also found missing glyphs with the GPU
terminal renderer in headless mobile WebKit, despite a populated terminal
buffer. The same production stream rendered legibly with the DOM renderer.
Touch devices now use DOM rendering by default; desktop acceleration remains
available. This was verified with mobile browser emulation, not a physical
iPhone GPU. Failed client-error reports also consume their own network
rejection, preventing a logging failure from recursively reporting itself.

Send shows progress, waits for the backend's acceptance result, then opens the
correct research or author terminal and follows its live output. Sending from a
paper page's Activity or Summary tab now targets the author. Failed delivery
keeps the input draft and displays the error. Long messages keep their complete
text in private workspace files; the native CLI receives a short reference that
can be inspected for safe Enter retry. Messages are saved to chat only after
verified submission or queuing. Busy state alone does not establish queuing.
Ordinary Send never uses cancellation keys. A narrow terminal can hide the
busy indicator, so uncertain queued-versus-started state is reported simply as
accepted after the input has been consumed.

Pause retains its intentional interruption behavior and writes a unique short
status marker with private pane ownership metadata. Resume removes only that
exact owned marker at the end of the input, then uses verified delivery. An
edited or legacy draft is preserved and a failed resume is reported explicitly;
the response separately identifies whether the research pause gate was cleared.
The supervisor also stops using unchanged terminal pixels as proof that a turn
is stuck: a cropped healthy turn receives a bounded visibility warning.

Regression coverage uses real xterm rendering in WebKit and Chromium, including
keyboard resize, reading history, Send, author routing, hung requests, disposal,
plain-text fallback, missed events, duplicate echoes, and reconnect ordering.
Private native-CLI probes exercise the submission/queue boundary separately;
these use synthetic content and loopback endpoints without paid inference.
The [native Send proof](verification/iphone-send-native-e2e-2026-10-29.json)
also exercises the real API and the default touch renderer with a real Codex
terminal. Its [completed phone conversation](screenshots/mobile-send-confirmed.png)
shows replies after three accepted messages without a page reload or browser
terminal Enter.
