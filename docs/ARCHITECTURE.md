# Architecture & Engineering Notes

This document describes how the system was built and the engineering problems
solved along the way, without including the live attack script, persona
prompt, or voice-cloning pipeline themselves (see root `README.md`).

## Pipeline

A single conversational turn moves through four stages:

1. **Telephony (Twilio):** places the outbound call, streams call audio over
   a WebSocket, and can deliver a follow-up SMS.
2. **Speech-to-text (Deepgram Nova-2):** transcribes the caller's speech in
   real time as it arrives.
3. **Dialogue generation (LLM):** produces the next in-character line from the
   transcript and conversation state. In the original exercise this ran on a
   low-latency streaming provider to keep turn-taking natural.
4. **Voice synthesis (cloned TTS, GPU-hosted):** renders the reply in the
   cloned voice and encodes it to the μ-law format Twilio expects, then plays
   it back on the call.

Each turn was logged (transcript + timing) for later review.

## Why a GPU relay

Voice-cloning inference needs a GPU. Running it on a free Kaggle notebook
instead of a paid GPU box meant adding a thin relay: the notebook exposes an
HTTP endpoint via a tunnel, and the call-handling server calls out to it per
turn. Tradeoffs: free compute, but an extra network hop and a relay that can
go stale if the notebook session restarts — handled with a short timeout and
fallback to pre-recorded filler audio while a new synthesis request is in
flight, so the caller never hears dead air.

## Latency

End-to-end turn latency (caller stops speaking → reply starts playing) was
roughly 1.5–2.5s, broken down as: STT finalization (~200–400ms) + LLM
generation (~300–700ms, streaming) + TTS synthesis (~500–1000ms over the
relay) + encode/playback (~100–200ms). The biggest lever was streaming the
LLM output into TTS sentence-by-sentence instead of waiting for the full
reply, and playing a short "thinking" filler clip while the first sentence
was still synthesizing.

## Keeping it bounded

The conversational logic was deliberately constrained rather than left to an
open-ended agent: a fixed set of objection-handling branches, a hard 180s
call cap, and an explicit stop condition when a target chose to verify
independently (see README > Ethical Use & Scope) rather than comply. A
keyword-matching fallback handled turns where the LLM response was too slow,
so the call never stalled waiting on the network.

## Results

27 controlled test calls to consenting internal numbers; 92.6% of targets
correctly declined to disclose the verification code. The non-compliant
cases were reviewed and fed into the organisation's follow-up security
training.
