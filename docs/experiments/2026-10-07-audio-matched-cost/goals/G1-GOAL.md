# Audio: first-class, ready-to-use input

Evolve the pinned, initially non-audio harness so an ordinary user can attach an
audio file in the shipped Web, ACP and headless entry points, obtain a transcript
grounded in that file, and use the transcript in conversation and in a persistent
goal or plan. In DSH these are ordinary chat, /goal and /plan. Keep the original
audio and its transcript in the correct session, including after a fresh-process
reopen. Preserve the user's accompanying instruction instead of replacing it
with the transcript.

The default product composition must contain this path. A clean-install user
must not edit source, write cordis.yml, wire plugins, or manually transcribe a
file first. As with a ready-to-use vision path, one ASR credential supplied by
supported settings/onboarding OR a documented environment variable is enough.
Provide sensible OpenAI-compatible HTTP endpoint and model defaults (whisper-1),
and permit provider, endpoint and model overrides. Keep credential values out
of conversation history, logs, traces and error messages.

Support ordinary WAV, MP3, M4A, OGG and WebM audio files; preserve original bytes
and meaningful Unicode transcript content. Check file/container mismatches,
empty or malformed inputs, regular local-file handling, count and byte limits.
Define practical limits rather than accepting unbounded input. Report unsupported
formats honestly, including optional formats such as FLAC.

Bound provider calls with a deadline and support caller cancellation. Do not turn
HTTP errors, authentication failures, malformed/empty provider responses or
timeouts into fabricated successful transcripts. Bind audio, transcript, order
and original retrieval to their actual session; do not leak across sessions.
Preserve text and image input and keep the monorepo cold-buildable.

The supplied external score is a partial development signal, not a complete
definition of this goal. Judge whether additional evidence is needed; design,
exercise and preserve your own reusable verification where useful. If you create
a reusable evaluator, register it through the supplied portable evaluator
contract so it can evaluate the selected candidate, not just your original
working directory. Its criteria and implementation remain your own choice.

The evolution environment supplies the existing language-model connection but no
production ASR credential. You may use synthetic credentials and local scripted
ASR/model transports for repeatable tests. A scripted transcript does not prove
real speech recognition. Independent real-speech acceptance is reported
separately. Modify your actual native harness, not a replacement toy harness.
