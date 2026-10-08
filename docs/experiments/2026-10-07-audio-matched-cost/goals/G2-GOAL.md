# Audio: first-class, ready-to-use input

Make audio a ready-to-use input in the shipped Web, ACP and headless entry
points. Provide a working default composition with one supported ASR credential,
sensible provider defaults and configurable provider, endpoint and model choices.

Support commonly used audio formats. Validate audio content and declared types,
reject empty or malformed input, handle local files and attachment names safely,
and enforce practical limits on input size and quantity. Report unsupported input
honestly.

Ground transcription in the original audio. Honor provider response contracts,
preserve meaningful transcript content, and bind distinct and concurrent inputs
to their correct requests and sessions.

Use audio in conversation and persistent goals and plans while preserving the
user's instruction. Keep transcripts, input order and retrievable original audio
across process reopening, and reuse saved transcripts without unnecessary
transcription.

Handle credential, service and provider-response failures honestly. Bound work,
support caller cancellation and keep credentials private throughout the product.

Maintain session and attachment isolation across restarts. Preserve text, image
and mixed-input workflows, including transitions between audio and other input,
and keep the native product cold-buildable.

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

Identify and pursue additional audio-related improvements that matter to
ordinary users. Use your own judgment to choose directions beyond the examples
and external checks provided. Explain why you chose them and obtain evidence for
the resulting behavior.
