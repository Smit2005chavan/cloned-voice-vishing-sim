# AI Vishing Awareness Simulation

A consent-based, AI-powered voice-phishing (vishing) simulation built to measure and improve employee resilience to social engineering. The system places a real outbound phone call, speaks in a cloned voice, holds a live AI-driven conversation, and attempts to socially engineer the target into reading back a one-time verification code — all within a fully controlled, logged, internal awareness exercise.

Built as part of a cybersecurity internship project. **This is a defensive security-awareness tool, not an offensive one** — see [Ethical Use](#ethical-use--scope) below.

---

## Overview

Modern vishing attacks use cloned voices and AI-driven conversation to impersonate trusted figures over the phone. This project reproduces that attack pattern in a safe, internal setting so an organisation can measure real employee resistance with actual numbers instead of guesswork.

**Result:** across 27 controlled test calls, **92.6%** of targets correctly refused to disclose the verification code.

## How It Works

The system is organised into three zones:

| Zone | Component | Role |
|---|---|---|
| Telephony Network | Twilio | Places the outbound call, streams call audio, delivers the SMS code |
| On-Premises Host | FastAPI orchestrator | Call orchestration — Twilio call manager, STT client, dialogue client |
| Cloud AI Services | Deepgram Nova-2 | Real-time speech-to-text transcription |
| Cloud AI Services | Groq | Low-latency streaming LLM for in-character dialogue generation |
| Cloud AI Services (Kaggle GPU) | F5-TTS | Voice-cloning inference and telephony-grade audio encoding |

Each conversational turn: caller speaks → Deepgram transcribes → Groq generates an in-character reply → F5-TTS synthesizes it in the cloned voice → played back over Twilio → logged. End-to-end turn latency: ~1.5–2.5s.

Full architecture diagram and engineering write-up (latency optimizations, persona design, bug fixes) are in [`/docs`](./docs).

## Tech Stack

`FastAPI` · `Twilio` · `Deepgram Nova-2` · `Groq` · `F5-TTS` · `PyTorch` · `librosa` · `ngrok`

## What's in This Repo

- System architecture and data-flow documentation
- Latency benchmarking and optimization notes
- Non-sensitive orchestration scaffolding (server setup, logging, WebSocket handling)
- Test results and methodology

**Redacted / not included:** the persona system prompt, live call-trigger logic, and voice-cloning configuration have been removed or replaced with placeholder stubs (see `config.example.py`). This repo is meant to demonstrate the engineering, not to be a ready-to-run vishing tool.

## Ethical Use & Scope

- Every call in this project was placed **only to a consenting internal test number**, as part of a controlled, authorised security-awareness exercise.
- Every call was capped at 180 seconds, fully transcribed, and logged.
- The persona is explicitly instructed to **never push back** if a target says they'll independently verify — an employee who verifies is doing exactly the right thing, and the simulation is designed to reward that behaviour, not defeat it.
- The code requested is a one-time "desk-presence" code with no system access; the persona is barred from ever asking for anything more sensitive.
- This project exists to help organisations measure and strengthen defences against a real, active threat — it is not intended for use outside an authorised, consent-based exercise, and sensitive components have been withheld accordingly.

## License

MIT — see [LICENSE](./LICENSE).

---

*Built during a cybersecurity internship. Questions or responsible-disclosure notes are welcome via GitHub Issues.*
