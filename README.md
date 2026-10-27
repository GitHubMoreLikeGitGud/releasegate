# ReleaseGate

**Pay when the work is proven, not when it is promised.**

A client pays up front, but the money only moves when the delivered file passes automated checks.

1. The client approves a PayPal **authorization** (a hold; the Orders v2 order uses `intent: AUTHORIZE`). Nothing is charged yet.
2. The freelancer uploads the deliverable.
3. ReleaseGate checks it against a checklist made from the job brief (length, resolution, loudness, silence, black frames, on-screen text).
4. **All required checks pass → the authorization is captured** (the freelancer is paid).
   **A required check fails → the authorization is voided** (the client is not charged).
   **A required check could not be verified → HOLD**: the hold stays open for a human decision, with the reason logged.
5. Every step is written to a tamper-evident **evidence log** in a *supported / unsupported / not verified* claim style.

> **Sandbox only.** ReleaseGate talks only to the PayPal **sandbox** (`https://api-m.sandbox.paypal.com`). It refuses any other PayPal host, so it cannot move real money.

## Modes (read this)

| | When | What it is |
|---|---|---|
| **PayPal sandbox** | `PAYPAL_CLIENT_ID` and `PAYPAL_CLIENT_SECRET` are set | Real calls to PayPal's free sandbox. **This is the mode for the real demo.** |
| **MOCK (local testing only)** | those variables are absent (or `RELEASEGATE_FORCE_MOCK=1`) | An in-process simulation of the same calls. No network, **not PayPal**, no payment is made. It exists so the checkers and the UI can be developed and tested without keys. The UI, CLI and every evidence entry say "MOCK". |

| Checklist | When |
|---|---|
| **AI-assisted** | `ANTHROPIC_API_KEY` is set. One small, cached call turns the brief into a checklist. |
| **Rule-based** | no key, or the call fails. Regex rules read lengths, resolutions, LUFS targets and quoted on-screen text from the brief. |

The AI output is validated against a fixed schema (known check types, clamped numbers) and merged over a rule-based baseline. It can only choose checks; it cannot touch payments or decide the outcome. The checks themselves are deterministic code.

## Requirements

- Python 3.10+ (standard library only for the core)
- `ffmpeg` and `ffprobe` on `PATH`
- Optional, for on-screen text checks: the `tesseract` CLI **or** `pip install rapidocr_onnxruntime`. Without either, text claims are reported **NOT VERIFIED** and the job goes to HOLD. They are never silently passed.
- Optional for tests: `pip install pytest`

## Run it

```bash
git clone <this repo> && cd releasegate

# optional: real sandbox + AI checklist (values are read from the environment only, never printed)
export PAYPAL_CLIENT_ID=...            # sandbox REST app client id
export PAYPAL_CLIENT_SECRET=...        # sandbox REST app secret
export ANTHROPIC_API_KEY=...           # optional; small model, cached
# export RELEASEGATE_MODEL=claude-haiku-4-5   # default

python -m releasegate status           # shows the mode, never the secrets
python -m releasegate serve            # http://127.0.0.1:8765
```

In the web page: **create job → authorize payment → upload the deliverable → run checks → read the evidence log and the capture/void result.** "Pre-flight" runs the checks without settling anything, so a freelancer can test a file first.

### Real sandbox demo

1. Create a sandbox REST app and two sandbox accounts (a *business* receiver and a *personal* buyer) in the PayPal developer dashboard. Use the app's sandbox client id and secret as above.
2. `python -m releasegate serve`, create a job, click **Authorize payment** (wallet login flow: written but not yet tested against PayPal; or click **Authorize with sandbox test card**, which is the path used in the demo), then **Open buyer approval** and log in as the sandbox buyer. PayPal returns you to `http://127.0.0.1:8765/paypal/return`, where ReleaseGate authorizes the order.
3. Generate sample files: `python -m releasegate samples ./samples` (good, short, silent, black, half-black, low-res), then upload `good.mp4` on one job (→ captured) and `short.mp4` on another (→ voided). Check both in the sandbox dashboard.

PayPal authorizations are only honored for a limited window (3 days, extendable to 29), so deliver and check inside it.

### CLI

```bash
python -m releasegate create --title "Teaser" --brief "A 10 second 720p video, around -23 LUFS" --amount 25.00
python -m releasegate pay JOB          # creates the AUTHORIZE order, prints the buyer approval URL
python -m releasegate paytest JOB      # sandbox only: authorize with the sandbox test card, no browser (demo and tests)
python -m releasegate authorize JOB    # after the buyer approved it
python -m releasegate deliver JOB file.mp4
python -m releasegate check JOB --dry  # pre-flight
python -m releasegate check JOB        # run checks, then capture / void / hold
python -m releasegate evidence JOB     # evidence log + hash-chain verification
python -m releasegate settle JOB capture --reason "..."   # human decision for a HOLD
python -m releasegate verify JOB             # read-only: ask PayPal what it says about the job
python -m releasegate demo             # local MOCK end-to-end run (good file captured, short file voided)
```

## What is checked

| Check | How | Fails when |
|---|---|---|
| duration | `ffprobe` | outside the min/max in the brief (±0.25 s) |
| resolution | `ffprobe` | long/short side below the request (either orientation) |
| audio present | `ffprobe` | no audio stream |
| loudness | `ffmpeg ebur128` integrated LUFS | outside the range |
| silence | `ffmpeg silencedetect` | more than the allowed share of silence |
| black frames | `ffmpeg blackdetect` | too much black, or one black run that is too long |
| on-screen text | OCR on 12 sampled frames | text never seen (or **NOT VERIFIED** if no OCR is installed) |

### Evidence log

`data/jobs/<id>/evidence.jsonl` is append-only; each line holds the SHA-256 of the previous line, so editing or deleting an earlier line is detectable (`evidence` command and the UI show the chain status). It records the file's SHA-256, each measurement with its expected value and method, the decision, and the PayPal order/authorization/capture ids. It is tamper-*evident*, not tamper-proof.

## Tests

```bash
pip install pytest
python -m pytest -q
```

43 tests, no network and no secrets: checker tests run on small videos generated with ffmpeg (a good file passes; too-short, silent, all-black, half-black, low-resolution, no-audio and non-media files fail); the PayPal layer is tested against a local fake of the sandbox API (request shapes, `intent: AUTHORIZE`, idempotency ids, token caching, error handling); the AI layer against a local fake (caching, schema validation, fallback); plus the job lifecycle, the web API and the hash chain.

## Limits and honest notes

- Checks measure *technical* properties. They cannot judge whether a video is good, original or on-brief creatively. Brief wording that the rules or the model cannot turn into a check is simply not checked, and the checklist shown to both sides says exactly what is.
- OCR can miss short or stylised on-screen text; a failed text check says so in its note.
- "Ends with a card that says X" is checked as "X appears in sampled frames", not "at the end".
- The brief is untrusted input; the AI layer ignores instructions inside it and its output is validated, but a client controls their own brief, so both sides should read the checklist before the hold is approved.
- Local web server binds to `127.0.0.1` and has no login; do not expose it to a network.
- This is a hackathon prototype: one currency per job, amount capped at 1000.00 (sandbox demo limit), JSON file storage.

## AI disclosure

- **Building:** AI assistants (including Anthropic's Claude, used through a coding assistant, plus an AI agent assistant) were used to help draft and write the code, tests and documents. The author directed the work and reviewed the result.
- **Runtime:** an optional small language model (Anthropic Claude Haiku 4.5, `claude-haiku-4-5`) turns a job brief into a checklist. Every pass/fail and capture/void decision is made by deterministic code, and the rule-based checklist works without any AI.
- The demo video is a real screen recording of the web UI against the PayPal sandbox, captioned, with no voice or music.

## What was tested, and what was not

- **Tested against the real PayPal sandbox:** creating an `AUTHORIZE` order with the sandbox test card (so the authorization hold is placed during order creation), capturing the authorization, voiding the authorization, and reading the order / authorization / capture status back with `releasegate verify`.
- **Not tested:** the normal wallet flow where a buyer logs in to a PayPal sandbox account and approves the order in the browser (`pay` then `authorize`). The code path exists and is unit-tested against a local fake of the API, but it has not been run against PayPal. The demo uses the test-card path.
- Not tested: authorization expiry and extension, refunds, currencies other than USD, concurrent use, or any production PayPal account.

## Tools and services

Python (standard library only for the core), ffmpeg / ffprobe, PayPal sandbox (Orders v2 and Payments v2 REST APIs), Anthropic Messages API with Claude Haiku 4.5 for the optional checklist, optional tesseract or RapidOCR for on-screen text, pytest for tests. AI assistants used while building: see the disclosure above.

## License

MIT, see `LICENSE`.
