# F5-TTS voice-clone relay -- runs a live server on a Kaggle GPU so
# ai_voice_demo_kaggle.py can synthesize real, dynamic replies during an
# actual call, in the cloned voice from the dataset in REF_AUDIO, in real
# time.
#
# HOW TO USE: run `python launch_kaggle_relay.py` from the project root. It
# pushes this file to Kaggle as a script kernel (kernel-metadata.json
# controls GPU/dataset attachment), waits for it to come up, and writes the
# resulting ngrok URL straight into local .env as TTS_RELAY_URL. Then run
# ai_voice_demo_kaggle.py as usual.
import os, subprocess, sys


def pip_install(args, label=None):
    label = label or " ".join(args)
    result = subprocess.run([sys.executable, "-m", "pip", "install"] + args,
                             capture_output=True, text=True)
    if result.returncode != 0:
        print(f"\n=== FAILED: {label} ===")
        print(result.stdout[-4000:])
        print(result.stderr[-4000:])
        return False
    print(f"OK: {label}")
    return True


_failed = []
for _pkg in ["fastapi", "uvicorn", "pyngrok", "soundfile", "librosa", "f5-tts"]:
    if not pip_install([_pkg]):
        _failed.append(_pkg)

_is_kaggle = os.path.exists("/kaggle/working")

# f5-tts's own install pulls in the latest torch (observed: 2.10.0+cu128),
# whose prebuilt wheels have dropped compiled kernels for Pascal (compute
# capability 6.0 -- what Kaggle's Tesla P100 is), which fails at inference
# time. Force-reinstall an older CUDA 12.1 build over whatever f5-tts just
# pulled in, BUT ONLY IF WE ARE ACTUALLY ON KAGGLE.
if _is_kaggle:
    if not pip_install(
        ["--force-reinstall", "--no-deps", "torch==2.4.1", "torchaudio==2.4.1",
         "torchvision==0.19.1", "--index-url", "https://download.pytorch.org/whl/cu121"],
        label="torch/torchaudio/torchvision (Pascal-compatible, matched cu121 build)",
    ):
        _failed.append("torch-pascal-compat")

print("\n===== INSTALL SUMMARY =====")
print("Failed:", _failed if _failed else "none -- all installed OK")
if _failed:
    raise SystemExit("Fix the failed package(s) above before continuing.")

import os, re, audioop, random
import numpy as np
import soundfile as sf
import librosa
import torch
from f5_tts.api import F5TTS
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
import uvicorn
from pyngrok import ngrok

RELAY_KEY = ""  # matches TTS_RELAY_KEY in local .env
NGROK_AUTHTOKEN = ""
# Separate from NGROK_AUTHTOKEN above -- this is an *API key*, which is the
# only credential that can list/stop OTHER agent sessions on your account
# (the authtoken only authenticates this one agent). Get one free at
# https://dashboard.ngrok.com/api-keys -- "New API Key". Leave blank to skip
# auto-cleanup and fall back to just killing the local agent + retrying.
NGROK_API_KEY = ""

# v5 reference clip -- same fluent source segment as v4, trimmed to 11.1s
# (was 14.8s). CRITICAL FIX: F5-TTS's preprocess_ref_audio_text() silently
# auto-crops any reference audio over 12 seconds, but does NOT shorten
# ref_text to match -- so v4's 14.8s clip got cropped internally while
# REF_TEXT still described the full 14.8s of words. The model computes its
# speaking-rate ratio as (cropped audio length) / (full ref_text length),
# which badly underestimates how long generated speech should be, and that
# same broken ratio applies to every single reply -- this, not the `speed`
# parameter, was the real cause of replies coming out ~3x too fast
# (measured: a 13-word reply as 1.7s of audio, ~460 wpm vs normal ~150).
# v3 (also 14s) had the same latent bug. Staying under 12s avoids the
# auto-crop entirely, so ref_text and ref_audio stay matched.
REF_AUDIO = "/kaggle/input/datasets/smitchavan/voice-clone-v5/reference_voice_v5.wav" if _is_kaggle else "reference_voice_v5.wav"
REF_TEXT  = ("Solutions, they have functionalities more or less same. So the most important "
             "differentiator should be that how do they orchestrate and how do they interact "
             "with other solutions.")

# The /synthesize loop swallows per-sentence exceptions (so one bad sentence
# doesn't kill the whole reply), which also means a wrong mount path would
# otherwise fail silently on every call -- fail fast at startup instead.
if not os.path.exists(REF_AUDIO):
    raise SystemExit(f"REF_AUDIO not found: {REF_AUDIO}")

device = "cuda" if torch.cuda.is_available() else "cpu"
print(f"Using device: {device}")
if device == "cuda":
    # Repeated fixed-shape inference (same model, similar-length generations
    # every call) is exactly what cudnn's autotuner is for -- lets it pick
    # faster kernels after the first couple of calls instead of using
    # generic ones for every request.
    torch.backends.cudnn.benchmark = True

# Diffusion steps -- the main speed/realism knob. Lower = faster, more
# synthetic-sounding. Reduced from 20 to 12 for live-call-feel latency:
# the quality ceiling on 8kHz mu-law telephony audio is the codec itself,
# not the diffusion model -- nuances above step ~12 are lost in the
# downsampling. Measured: 20 steps ~5-6s/sentence, 12 steps ~3-3.5s.
NFE_STEP = 12

print("Loading F5-TTS...")
# Explicit device= rather than relying on F5TTS()'s auto-detect -- makes
# sure this is actually running on the GPU, not silently falling back to
# CPU (which would look like "it's just slow" with no obvious error).
model = F5TTS(device=device)
print("Ready.")

# Self-warmup, before the tunnel opens -- the first call against a fresh
# reference clip pays a one-time cost (preprocessing REF_AUDIO, cudnn
# autotuning) that later calls don't. Eating that cost here, during startup
# you're already waiting through, keeps it off the live call's first turn.
print("Warming up (one-time reference-audio preprocessing + kernel autotune)...")
import time as _time
_t0 = _time.time()
try:
    with torch.inference_mode():
        model.infer(
            ref_file=REF_AUDIO, ref_text=REF_TEXT, gen_text="Warming up.",
            nfe_step=NFE_STEP, cfg_strength=2.0, sway_sampling_coef=-1.0,
            speed=1.0, cross_fade_duration=0.15, remove_silence=False,
        )
    print(f"Warmup done in {_time.time() - _t0:.1f}s -- later calls should be faster than this.")
except Exception as e:
    print(f"⚠️  Warmup failed (non-fatal, first real call will pay this cost instead): {e}")

# torch.compile -- JIT-compiles the model's forward pass for repeated
# fixed-shape inference (same model, similar-length generations every call).
# The first compiled call is slower (tracing), but every subsequent call
# benefits from persistent kernel fusion. Requires PyTorch 2.x.
try:
    if hasattr(torch, "compile"):
        model.ema_model = torch.compile(model.ema_model, mode="reduce-overhead")
        print("torch.compile applied to model (reduce-overhead mode).")
except Exception as e:
    print(f"⚠️  torch.compile skipped (non-fatal): {e}")


def audio_array_to_mulaw_8k(audio: np.ndarray, sr: int) -> bytes:
    """Convert an F5-TTS float32 waveform to raw 8kHz mu-law bytes (no WAV
    header) -- exactly the format Twilio's Media Streams expects."""
    data = np.asarray(audio, dtype=np.float32)
    if data.ndim > 1:
        data = data.mean(axis=1)
    if sr != 8000:
        data = librosa.resample(data, orig_sr=sr, target_sr=8000)
    # F5-TTS's raw output loudness isn't guaranteed consistent between
    # generations, and peak-only normalization (tried first) didn't fix
    # "can't hear you" reports during live testing -- peak-matching only
    # guarantees the single loudest sample (a brief consonant transient)
    # hits near full scale, it says nothing about the AVERAGE loudness a
    # listener actually perceives, which is dominated by the quieter vowel
    # body of speech. Normalize by RMS (average energy) instead so overall
    # perceived volume is consistent and loud enough, with a peak cap
    # only as a safety limit against clipping.
    rms = float(np.sqrt(np.mean(np.square(data)))) if data.size else 0.0
    if rms > 1e-6:
        TARGET_RMS = 0.18  # empirically loud-but-clean for telephony mu-law
        data = data * (TARGET_RMS / rms)
        peak = float(np.max(np.abs(data)))
        if peak > 0.98:
            data = data * (0.98 / peak)
    # Each sentence is generated and trimmed independently, then concatenated
    # back-to-back on the client as one continuous byte stream (/synthesize
    # streams sentence-by-sentence, and the caller never re-splits it). Trim
    # cuts right up to real signal on both ends, so without this, sentence N's
    # last sample jumps straight to sentence N+1's first sample -- an abrupt
    # amplitude discontinuity that's audible as a click/pop between sentences.
    # A short fade in/out brings both edges toward zero so no seam is audible.
    _fade_n = min(len(data) // 4, int(0.008 * 8000))  # ~8ms, capped for short clips
    if _fade_n > 1:
        ramp = np.linspace(0.0, 1.0, _fade_n, dtype=np.float32)
        data[:_fade_n] *= ramp
        data[-_fade_n:] *= ramp[::-1]
    pcm16 = (np.clip(data, -1, 1) * 32767.0).astype(np.int16)
    # audioop is the actual G.711 reference implementation (bit-exact, same
    # as what Twilio's decoder expects) -- still built into Python 3.12,
    # which is what this Kaggle image runs.
    return audioop.lin2ulaw(pcm16.tobytes(), 2)


app = FastAPI()

_SENT_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")

# Clause-level split — commas, ellipses, dashes, semicolons. These are
# the natural breath points where a senior person would pause briefly.
# F5-TTS already respects punctuation somewhat, but inserting explicit
# silence gaps between clauses makes the pacing noticeably more human
# and deliberate — the "senior employee" cadence the caller should hear.
_CLAUSE_SPLIT_RE = re.compile(r'(?<=[,;])\s+|\.\.\.|\s+[—–-]\s+')


def _split_sentences(text: str) -> list[str]:
    parts = [p.strip() for p in _SENT_SPLIT_RE.split(text) if p.strip()]
    return parts or [text]


def _split_clauses(sentence: str) -> list[str]:
    """Split a sentence into clauses at commas, ellipses, and dashes.
    Returns a list of clause strings. Single-clause sentences return
    a one-element list (no extra pauses added)."""
    parts = [p.strip() for p in _CLAUSE_SPLIT_RE.split(sentence) if p.strip()]
    return parts if parts else [sentence]


def _silence_mulaw(seconds: float, sr: int = 8000) -> bytes:
    """Explicit silence gap between sentences within one multi-sentence
    reply. The per-sentence fade in audio_array_to_mulaw_8k only ramps the
    last/first ~8ms of each clip toward zero -- with sentences concatenated
    back-to-back and no gap, that reads as a ~16ms notch, not a real pause,
    which can sound like a stutter on genuinely multi-sentence lines (the
    opening/closing greeting, or any turn where the model ignores the
    one-sentence-per-reply instruction). Only used between sentences, never
    on the single-sentence path most turns take."""
    n = int(seconds * sr)
    return audioop.lin2ulaw(np.zeros(n, dtype=np.int16).tobytes(), 2)


class SynthRequest(BaseModel):
    text: str


@app.post("/synthesize")
async def synthesize(req: SynthRequest, x_relay_key: str = Header(default="")):
    if x_relay_key != RELAY_KEY:
        raise HTTPException(status_code=401, detail="bad relay key")

    text = req.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="empty text")

    # Most replies are one short sentence (see SYSTEM_PROMPT on the local
    # side), so this rarely kicks in -- but for multi-sentence lines (the
    # opening/closing greeting) it lets the caller start hearing sentence 1
    # while sentence 2+ are still being generated, instead of one long wait
    # for the whole reply before any audio streams back.
    sentences = _split_sentences(text)
    multi = len(sentences) > 1

    def gen():
        for i, sentence in enumerate(sentences):
            try:
                _t_infer_start = _time.perf_counter()
                # Synthesize the FULL sentence as one unit — no clause
                # splitting. F5-TTS already respects commas/ellipses
                # internally for natural pausing. Splitting into clauses
                # and stitching back together caused audible breaks.
                with torch.inference_mode():
                    wav, sr, _ = model.infer(
                        ref_file=REF_AUDIO,
                        ref_text=REF_TEXT,
                        gen_text=sentence,
                        nfe_step=NFE_STEP,
                        cfg_strength=2.0,
                        sway_sampling_coef=-1.0,
                        speed=1.15,
                        cross_fade_duration=0.15,
                        remove_silence=False,
                    )
                sentence_mulaw = audio_array_to_mulaw_8k(wav, sr)
                _t_infer = _time.perf_counter() - _t_infer_start
                _out_seconds = len(sentence_mulaw) / 8000.0
                _words = len(sentence.split())
                _wpm = _words / (_out_seconds / 60.0) if _out_seconds > 0 else 0
                print(f"⏱  sentence {i}: {_words} words -> "
                      f"infer {_t_infer:.2f}s, audio {_out_seconds:.2f}s ({_wpm:.0f} wpm)")
                yield sentence_mulaw
                if multi and i < len(sentences) - 1:
                    # Brief inter-sentence gap — senior pacing
                    yield _silence_mulaw(0.12)
            except Exception as e:
                print(f"synth error on sentence {i} ({sentence!r}): {e}")

    return StreamingResponse(gen(), media_type="audio/basic")


@app.get("/health")
async def health():
    gpu_name = torch.cuda.get_device_name(0) if torch.cuda.is_available() else None
    return {"status": "ok", "device": device, "gpu": gpu_name}


ngrok.set_auth_token(NGROK_AUTHTOKEN)

def _kill_remote_ngrok_sessions(api_key: str):
    # ngrok.kill() below only stops a LOCAL agent process -- if the tunnel
    # that's "already online" belongs to a different Kaggle session/kernel
    # entirely, that's a separate process we have no local handle on. The
    # only way to stop it is ngrok's REST API (needs an API key, which is a
    # different credential from NGROK_AUTHTOKEN -- see note above).
    import httpx as _hx
    headers = {"Authorization": f"Bearer {api_key}", "Ngrok-Version": "2"}
    try:
        r = _hx.get("https://api.ngrok.com/tunnel_sessions", headers=headers, timeout=15)
        r.raise_for_status()
        sessions = r.json().get("tunnel_sessions", [])
        if not sessions:
            print("No existing ngrok tunnel sessions on this account.")
            return
        for s in sessions:
            sid = s["id"]
            print(f"Stopping stale ngrok session {sid} (region={s.get('region', '?')}, "
                  f"started={s.get('started_at', '?')})")
            try:
                _hx.post(f"https://api.ngrok.com/tunnel_sessions/{sid}/stop",
                         headers=headers, timeout=15)
            except Exception as e:
                print(f"  ⚠️  could not stop {sid}: {e}")
    except Exception as e:
        print(f"⚠️  Could not query ngrok API (check NGROK_API_KEY): {e}")

def _connect_ngrok(port, retries=3, delay=3):
    # A free ngrok account allows only one online tunnel. If a previous run's
    # kernel wasn't cleanly stopped, its tunnel is still registered and a new
    # connect() collides with it (ERR_NGROK_334) -- kill any local agent,
    # stop any remote session via the API (if a key is set), and retry a
    # few times before giving up.
    import time
    if NGROK_API_KEY:
        _kill_remote_ngrok_sessions(NGROK_API_KEY)
        time.sleep(2)
    last_err = None
    for attempt in range(retries):
        try:
            ngrok.kill()
            time.sleep(1)
            return ngrok.connect(port, "http").public_url
        except Exception as e:
            last_err = e
            print(f"⚠️  ngrok attempt {attempt + 1}/{retries} failed: {e}")
            if NGROK_API_KEY:
                _kill_remote_ngrok_sessions(NGROK_API_KEY)
            time.sleep(delay)
    raise SystemExit(
        "Could not open an ngrok tunnel -- a previous session's tunnel is likely "
        "still online. Set NGROK_API_KEY above (https://dashboard.ngrok.com/api-keys) "
        "so this can auto-stop it, or stop it manually at "
        "https://dashboard.ngrok.com/agents, then rerun this cell.\n"
        f"Last error: {last_err}"
    )

public_url = _connect_ngrok(8000)
print("=" * 64)
print(f"Relay ready at: {public_url}")
print(f"Put this in your local .env:")
print(f"  TTS_RELAY_URL={public_url}")
print(f"  TTS_RELAY_KEY={RELAY_KEY}")
print("=" * 64)

# Pushed via the Kaggle API as a script kernel (not pasted into a notebook
# cell), so there's no pre-existing event loop to piggyback a top-level
# await on -- asyncio.run() is the normal, correct way to drive this here.
import asyncio

async def _main():
    _config = uvicorn.Config(app, host="0.0.0.0", port=8000)
    _server = uvicorn.Server(_config)
    await _server.serve()

asyncio.run(_main())
