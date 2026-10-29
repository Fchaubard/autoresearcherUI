# Gemini 4 Carbon availability

Verified 2026-10-29 against the saved Google API credential using a read-only
`GET https://generativelanguage.googleapis.com/v1beta/models?pageSize=1000`.
The complete model list returned HTTP 200, 60 entries and no Carbon model.
Its only Gemini 4 entry was `models/gemini-4-argon`.

Google's [October 28 announcement](https://blog.google/innovation-and-ai/models-and-research/gemini-models/introducing-gemini-4-carbon/)
describes initial access through the Fairwind program and a later developer
release. The [official Gemini API catalog](https://ai.google.dev/gemini-api/docs/models)
does not publish a Carbon endpoint ID as of this check.

Carbon has therefore **not been enabled or inference-tested**. The application
does not invent an endpoint, select another Gemini model on Carbon's behalf, or
change any existing research selection. Once Google exposes an official API ID
to this account, add that exact ID and run the bounded advisor, chat, coding and
effort probes before advertising support. Model-list enumeration is an access
check, not evidence that any Carbon role has passed.
