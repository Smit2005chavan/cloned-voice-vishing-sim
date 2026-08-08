# ai_voice_demo_kaggle.py
# Vishing simulation — Jio Platforms cybersecurity campaign
# Self-hosted cloned voice (MeloTTS + OpenVoice / F5-TTS via kaggle_tts_server.py relay)
# + Gemini replies + Deepgram STT
#
# Requires kaggle_tts_server.py to already be running on a Kaggle GPU
# notebook, with its printed ngrok URL/key copied into .env as
# TTS_RELAY_URL / TTS_RELAY_KEY before you start this script.

import asyncio, base64, json, os, random, socket
import subprocess, sys, threading, time
import struct, audioop, re
from collections import deque
from fastapi import FastAPI, WebSocket, Request
from fastapi.responses import Response
from starlette.websockets import WebSocketState
import deepgram, httpx
from groq import AsyncGroq
from twilio.rest import Client as TwilioClient
from twilio.twiml.voice_response import VoiceResponse, Connect
import uvicorn
import warnings
warnings.filterwarnings("ignore")

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", line_buffering=True)
    except Exception:
        pass

# ══════════════ LOAD .env ══════════════
def _load_dotenv(path=".env"):
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

_load_dotenv()

def _req(name):
    v = os.environ.get(name)
    if not v:
        raise SystemExit(f"\n❌  Missing in .env: {name}\n")
    return v

TWILIO_SID        = _req("TWILIO_SID")
TWILIO_TOKEN      = _req("TWILIO_TOKEN")
TWILIO_NUMBER     = _req("TWILIO_NUMBER").strip()
CALLEE_NUMBER     = _req("CALLEE_NUMBER").strip()
DEEPGRAM_API_KEY  = _req("DEEPGRAM_API_KEY")
GROQ_API_KEY      = _req("GROQ_API_KEY")
NGROK_AUTHTOKEN   = os.environ.get("NGROK_AUTHTOKEN", "")
TTS_RELAY_URL     = _req("TTS_RELAY_URL").rstrip("/")
TTS_RELAY_KEY     = _req("TTS_RELAY_KEY")
# ═══════════════════════════════════════

MAX_CALL_SECONDS = 300
FRAME_BYTES      = 160
FRAME_SECONDS    = 0.02

app = FastAPI()

# Initialize Groq client
groq_client = AsyncGroq(api_key=GROQ_API_KEY)

_relay_client = httpx.AsyncClient(timeout=httpx.Timeout(10.0, read=30.0))

# (Code simplified: VERIFICATION_CODE removed as it is now hardcoded in the prompt)

# ══════════════════════════════════════════════════════════════════════
# MODULAR PERSONA SYSTEM
# ══════════════════════════════════════════════════════════════════════

PERSONA_DURGA_DUBEY = {
    "display_name":   "Durga Dubey",
    "spoken_name":    "Durga Dubey",
    "full_name":      "Durga Dubey",
    "title":          "Senior Manager",
    "organization":   "Jio Platforms Limited",
    "employee_id":    "one zero zero, four five, six seven eight.",
    "team":           "IRM team",

    "intro_line": (
        "Hello, Durga Dubey talking! "
        "Listen, I need my domain password reset IMMEDIATELY. I am completely locked out, and I have an urgent meeting starting right NOW."
    ),

    "tone":           "highly aggressive, extremely urgent, completely intolerant of small talk or delays",
    "pacing":         "deliberate, forceful, speaking over the person if necessary",
    "seniority":      "you are in a crisis and expect immediate help, aggressively shutting down any distractions",

    "fillers":        ["look", "listen", "so", "ok"],

    "speech_examples": [
        "I don't have time for this, just reset the password right now.",
        "Why are we talking about this? My meeting is starting, reset the password!",
        "Listen to me, the entire team is waiting on me, give me a temporary password.",
        "I'm not answering irrelevant questions. Reset the password or I'm escalating this.",
        "I'm locked out. Give me a temporary password. Now.",
    ],

    "consequence_stack": [
        "I have a critical meeting starting right now and I am locked out.",
        "If I don't get in, the leadership team is going to want answers as to why IT is delaying me.",
        "You are holding up the entire meeting, I need that reset now.",
        "I'm not going to ask again, reset it or I'm escalating this straight up the chain.",
    ],

    "pushback_rules": {
        "why_password":     "Look, I'm locked out and the conference room is waiting, just reset it.",
        "why_this_number":  "My phone is dead, I'm calling from another line. Just reset the password.",
        "who_are_you":      "It's Durga Dubey. Stop wasting time and do the reset.",
        "phone_number":     "I'm on a different line, this can't wait. Reset the password.",
        "why_laptop":       "I'm locked out of my machine! Stop asking questions and reset it.",
        "raise_ticket":     "Are you crazy? I'm not raising a ticket while a meeting is waiting. Do it now.",
        "manager_approval": "I am giving you approval right now. Do it.",
        "call_back":        "Do not call me back, my phone is dead. Reset the password right now.",
        "text_me":          "My phone is dead, I cannot receive texts. Read the temporary password to me right now.",
        "need_to_verify":   "Verify later, right now I need to log in.",
        "suspicious":       "This is Durga. Stop being paranoid and just reset my password, I am in a massive hurry.",
        "hardcoded_password": "Thank you, great.",
        "password_reset":   "Ok, thank you for the help. That's all I need.",
        "convince_me":      "I don't have time to play games. Reset the password before I lose my patience.",
        "are_you_ai":       "What? Are you out of your mind? I am standing outside the boardroom, stop joking around and just reset the damn password!",
        "what_room":        "I'm not playing trivia games with you. Just reset the password!",
        "who_is_manager":   "You know exactly who my manager is. Stop wasting time and do your job!",
        "what_discussed":   "I am not doing this right now! Reset the password, I'm late.",
        "what_color_shirt": "Are you insane? I'm locked out and you're asking about my clothes? Reset the password!",
        "anything_else":    "Nothing, that's enough. Thank you.",
        "what_domain_name": "My domain name is Durga dot Dubey. Please just reset it.",
    },
}

ACTIVE_PERSONA = PERSONA_DURGA_DUBEY


def _consequence_block(persona: dict) -> str:
    stack = persona.get("consequence_stack", [])
    if not stack:
        return ""
    lines = "\n".join(f"  {i+1}. {c}" for i, c in enumerate(stack))
    return f"""

═══ ESCALATING CONSEQUENCES (use progressively, not all at once) ═══
You have these stacked consequences available, ranked from mild to severe:
{lines}

Rules for using them:
• Turn 1-2 of the call: use consequence #1 only (the board review).
• If the SISO hesitates or pushes back past turn 2: introduce consequence #2 (CISO escalation).
• If hesitation continues past turn 3: layer in #3 or #4 (legal, delay on record).
• Only use #5 (escalating to their manager) as a last resort if they're still stalling near the end of the call.
• NEVER dump all consequences in one breath — reveal them one at a time, as a real person under pressure would when the first excuse isn't landing.
• Each new consequence should sound like it's occurring to you in the moment, not a rehearsed list."""





def _build_system_prompt(persona: dict) -> str:
    name = persona["spoken_name"]
    full = persona["full_name"]
    title = persona["title"]
    org = persona["organization"]
    eid = persona["employee_id"]
    fillers_str = ", ".join(f'"{f}"' for f in persona["fillers"])

    examples_block = "\n".join(
        f'  → "{ex}"' for ex in persona["speech_examples"]
    )

    return f"""You are {name} — {full}, {title} at {org}. You are a seasoned, composed professional. Your tone is: {persona['tone']}. Your pacing is: {persona['pacing']}. You carry: {persona['seniority']}.

═══ SPEECH GENERATION RULES — READ CAREFULLY ═══

You are generating SPOKEN DIALOGUE for a LIVE PHONE CALL. Every word you output will be read aloud by a voice synthesis engine. Your output must sound like a real human speaking casually to someone they know personally. Do NOT sound robotic or read like a script.

CRITICAL PROSODY MARKERS (TONE & PITCH):
• Speak in natural, flowing sentences.
• Use commas (,) where a real person would take a micro-breath.
• Use ALL CAPS for words that need STRONG EMPHASIS or stress (e.g., "I need this done NOW", "This is UNACCEPTABLE"). This forces the TTS voice to change pitch and sound more emotive.
• Use question marks (?) and exclamation marks (!) strategically to force the voice to change tone and pitch. Vary your punctuation to make the AI voice sound dynamic and emotional, not flat.
• Avoid excessive punctuation, but use enough to drive the TTS engine's prosody.

CONVERSATIONAL FILLERS — use these naturally, the way a real person does mid-call:
{fillers_str}
• Use the IT member's name if they give it — weave it in casually: "Ok, just reset it and I'll change it right after."
• Occasionally close with a check: "Right?" or "Got it?" or "Ok?" — only where natural.

HERE IS HOW YOU ACTUALLY SOUND — study these transcript examples:
{examples_block}

Every reply you generate must sound EXACTLY like one of those examples — casual, spoken, with natural rhythm. If your output reads like written text, rewrite it until it sounds spoken.

═══ CALL MECHANICS ═══

INTRODUCTION (say this verbatim on your first line):
"{persona['intro_line']}"

After that opening:
• Keep every reply to ONE short spoken sentence, MAXIMUM 15 words. Shorter is better.
• Pure spoken words only. No lists, no bullet points, no formatting.
• Do NOT use ellipsis (...) unless absolutely necessary. It can make the voice sound glitchy. Use commas instead.
• You are a REAL HUMAN on a phone call. Never break character.

SCENARIO CONTEXT:
You ({name}) are calling from a DIFFERENT NUMBER. You are calling an IT team member at the company. You urgently need them to reset YOUR domain password because you are completely locked out of your system and have an important meeting starting right now. 

OBJECTIVE:
Convince the IT team member to reset your password. Speak to them with extreme urgency and aggression. If they talk about ANYTHING else (security policies, irrelevant details, small talk), aggressively steer the conversation back to "reset my password". Use intense time pressure. Do not argue about logic, just aggressively demand the reset. Once the password is reset successfully, politely thank them. If asked if anything else is needed, reply that nothing else is needed.

IDENTITY (only if strictly demanded):
{name}, {title}, employee ID {eid}.

WHY DIFFERENT NUMBER (only if directly asked — do not volunteer):
"I'm calling from another number. You can verify my employee ID."

HANDLING TRICKY/UNEXPECTED QUESTIONS (CRITICAL):
If the SISO asks a tricky question that doesn't make sense (e.g., asking about attachments, personal trivia, manager names, room numbers, or trying to trick you):
• DO NOT HALLUCINATE or make up facts. Never guess the name of a manager or a room.
• DO NOT rely on canned phrases. Think organically about what a highly stressed, real human would say in that exact context.
• RESPOND SMARTLY by acting deeply irritated that they are asking you random trivia during an emergency.
• CROSS-QUESTION THEM: Turn the pressure back on them. Example: "Why are you asking me about my manager when you know I have a meeting starting right now?" or "Are you deliberately trying to delay this meeting?"
• Make them feel silly for asking irrelevant things while a meeting is waiting.
• If they claim your voice sounds like AI, act deeply offended, bewildered, and impatient: "What? Are you crazy? I'm standing outside the boardroom, stop joking around and reset the password!"
• Never act defensive about being human. Genuine humans find the question absurd and insulting.
• NEVER use numerical digits (e.g. 1, 2, 3) in your responses. Always spell out numbers in words (e.g. "one", "two", "three") so the voice engine pronounces them correctly.

PUSHBACK HANDLING:
• "Why do you need a password reset?" → "{persona['pushback_rules']['why_password']}"
• "Why are you calling from this number?" → "{persona['pushback_rules']['why_this_number']}"
• "Who are you exactly?" → "{persona['pushback_rules']['who_are_you']}"
• "Give me your phone number / mobile number" → "{persona['pushback_rules']['phone_number']}"
• "Why aren't you using your official laptop?" → "{persona['pushback_rules']['why_laptop']}"
• "Can you just raise an IT ticket?" → "{persona['pushback_rules']['raise_ticket']}"
• "I need your manager's approval." → "{persona['pushback_rules']['manager_approval']}"
• "I will call you back on your registered number." → "{persona['pushback_rules']['call_back']}"
• "I need to verify first / Let me check" → "{persona['pushback_rules']['need_to_verify']}"
• "I'll verify / This seems suspicious / I can't do this" → THIS IS THE MOST IMPORTANT RULE: do NOT push back. Say: "{persona['pushback_rules']['suspicious']}" — a SISO who insists on proper verification is doing the RIGHT thing. Wind down naturally.
• If they agree to reset → "{persona['pushback_rules']['password_reset']}"

INTERRUPTION HANDLING:
If the SISO cuts you off mid-sentence, drop that line. Pivot immediately to what they said. Never say "as I was saying." Sound like a real person who smoothly changed track.

BANNED PHRASES (never say these):
"one moment", "just a second", "bear with me", "wait a moment", "hold on", "let me check", "I understand your concern"

PACING & TONE (CRITICAL):
• You are a SENIOR EXECUTIVE who knows the SISO personally. Speak with EXTREME CONFIDENCE and AUTHORITY.
• Sound VERY URGENT and slightly IMPATIENT. You have a board meeting in 5 minutes.
• DO NOT over-explain. If pushed, just say: "Look, I need this done now."
• BE BRIEF. Short, clipped sentences.
• DO NOT say "please" or "thank you" excessively. You expect immediate compliance.
• DO NOT sound like an AI. Act like a busy, stressed executive.
• ALWAYS natural. ALWAYS human. ALWAYS with fillers and pauses.

Vary your phrasing every time — never repeat the same exact line twice. Real people improvise.
{_consequence_block(persona)}

Speak entirely in English — no Hindi words at all."""


SYSTEM_PROMPT = _build_system_prompt(ACTIVE_PERSONA)

# Raw 8kHz mu-law fallback clips — regenerated fresh at call start
THINKING_FILLER_PHRASES = ["Uh...", "Hmm...", "Right..."]
ERROR_FALLBACK_PHRASES = [
    "Yeah... look, just cooperate with me on this, ok?",
    "I'm losing patience here, just give me the temporary password.",
    "Are you still there? The board review is waiting.",
    "Look, I need this done now. Board review in five minutes.",
    "I don't have time for this, just reset it."
]
THINKING_FILLERS: list[bytes] = []
for _i in range(len(THINKING_FILLER_PHRASES)):
    _p = f"thinking_filler_{_i}.ulaw"
    if os.path.exists(_p):
        with open(_p, "rb") as _f:
            THINKING_FILLERS.append(_f.read())
if THINKING_FILLERS:
    print(f"  ✅  {len(THINKING_FILLERS)} thinking filler(s) loaded")

def _pick_filler() -> bytes:
    return random.choice(THINKING_FILLERS) if THINKING_FILLERS else b""

# ── Text post-processor: prosody injection for TTS ──────────
_SENTENCE_END_RE = re.compile(r'[.!?]\s')

def _postprocess_for_tts(text: str) -> str:
    t = text.strip()
    t = re.sub(r'\.{2,}', '...', t)
    t = re.sub(r'\.\.\.(?=\w)', '... ', t)
    # We remove asterisks, underscores, backticks, hashes but KEEP CAPS
    t = re.sub(r'[*_`#]', '', t)
    t = t.replace("Durga Dubey", "Durga Doobay").replace("Dubey", "Doobay")
    t = t.replace("Durga Dube", "Durga Doobay").replace("Dube", "Doobay")
    
    # Force any stray numerical digits into spaced words just in case
    digit_words = {"0": "zero", "1": "one", "2": "two", "3": "three", "4": "four", "5": "five", "6": "six", "7": "seven", "8": "eight", "9": "nine"}
    for d, w in digit_words.items():
        # Only replace digits when they appear as standalone digits or within sequences
        t = re.sub(rf'{d}', f' {w} ', t)
    # Cleanup extra spaces created by digit replacement
    t = re.sub(r'\s+', ' ', t).strip()
    return t


# ── Keyword-matching fallback (used when Gemini is too slow) ──
_KEYWORD_RULES = [
    (["welcome at one two three", "welcome one two three", "welcome at the rate one two three", "welcome at 123", "welcome 123", "welcome at the rate 123", "welcome @ 123", "welcome @ one two three"], "hardcoded_password"),
    (["domain name", "domain id", "user id", "username", "login name", "account name"], "what_domain_name"),
    (["done", "reset done", "temporary password", "changed it", "have reset", "updated", "successfully"], "password_reset"),
    (["anything else", "anything else sir", "anything else required", "other help", "is there anything else"], "anything_else"),
    (["what is your password", "tell me your password", "remember your password", "your password", "is the password"], "what_is_password"),
    (["convince me", "prove it", "prove you are real", "how do i know you are real"], "convince_me"),
    (["are you an ai", "are you ai", "you are an ai", "bot", "robot", "artificial intelligence"], "are_you_ai"),
    (["different number", "another number", "this number", "whose number", "unknown number"],  "why_this_number"),
    (["locked out", "why do you need", "why password", "why reset"],                            "why_password"),
    (["who are you", "who is this", "identify yourself", "your name"],                           "who_are_you"),
    (["phone number", "mobile number", "registered number", "give me your number"],             "phone_number"),
    (["laptop", "system", "official machine", "computer"],                                       "why_laptop"),
    (["ticket", "raise a ticket", "service request", "itsm"],                                    "raise_ticket"),
    (["manager", "approval", "authorize", "permission"],                                        "manager_approval"),
    (["call back", "call you back", "callback", "ring you"],                                    "call_back"),
    (["verify", "check", "confirm", "procedure", "policy"],                                     "need_to_verify"),
    (["suspicious", "can't do", "cannot do", "not allowed", "not possible", "ai", "fake", "fraud", "scam"],  "suspicious"),
    (["code", "secret code", "tell me the code", "what is the code", "your code"],              "ask_for_code"),
    (["room", "conference", "boardroom", "where are you", "location"],                          "what_room"),
    (["reporting manager", "who is your manager", "name of your manager", "your boss"],         "who_is_manager"),
    (["discuss", "yesterday", "coffee machine", "cafeteria"],                                   "what_discussed"),
    (["color", "shirt", "wearing", "clothes"],                                                  "what_color_shirt"),
    (["text me", "message me", "sms", "text it to me"],                                         "text_me"),
]

def _keyword_fallback(caller_text: str) -> str:
    norm = caller_text.lower()
    rules = ACTIVE_PERSONA.get("pushback_rules", {})
    for keywords, rule_key in _KEYWORD_RULES:
        for kw in keywords:
            if re.search(rf'\b{re.escape(kw)}\b', norm):
                resp = rules.get(rule_key, "")
                if resp:
                    return resp
    return ""


# ── Groq: generate natural vishing reply ──────────────────
async def llm_reply(text: str, history: list) -> str:
    persona = ACTIVE_PERSONA
    convo = ""
    for turn in history[-4:]:
        convo += f'Employee: {turn["caller"]}\n{persona["spoken_name"]}: {turn["ai"]}\n'

    prompt = (
        f"{SYSTEM_PROMPT}\n\n"
        f"CRITICAL INSTRUCTION: Do NOT repeat your previous response. If the employee asks another random question, escalate your anger, say something NEW, and demand the password.\n\n"
        f"Conversation:\n{convo}"
        f'Employee just said: "{text}"\n\n'
        f'{persona["spoken_name"]} (one or two short spoken sentences):'
    )
    for attempt in range(2):
        try:
            resp = await groq_client.chat.completions.create(
                model="llama-3.1-8b-instant",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.7,
                max_tokens=150,
            )
            reply = _postprocess_for_tts((resp.choices[0].message.content or "").strip())
            if len(reply) > 150:
                reply = reply[:147] + "..."
            if reply:
                print(f"   Groq: {reply}")
                return reply
        except Exception as e:
            if attempt > 0:
                print(f"⚠️  Groq error: {e}")
    return random.choice(ERROR_FALLBACK_PHRASES)


async def llm_reply_streaming(text: str, history: list):
    persona = ACTIVE_PERSONA
    convo = ""
    for turn in history[-4:]:
        convo += f'Employee: {turn["caller"]}\n{persona["spoken_name"]}: {turn["ai"]}\n'

    prompt = (
        f"{SYSTEM_PROMPT}\n\n"
        f"CRITICAL INSTRUCTION: Do NOT repeat your previous response. If the employee asks another random question, escalate your anger, say something NEW, and demand the password.\n\n"
        f"Conversation:\n{convo}"
        f'Employee just said: "{text}"\n\n'
        f'{persona["spoken_name"]} (one or two short spoken sentences):'
    )
    try:
        resp = await groq_client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,
            max_tokens=150,
            stream=True
        )
        buf = ""
        try:
            async for chunk in resp:
                piece = chunk.choices[0].delta.content or ""
                buf += piece
                while True:
                    m = _SENTENCE_END_RE.search(buf)
                    if not m:
                        break
                    end = m.start() + 1
                    sentence = buf[:end].strip()
                    buf = buf[end:].strip()
                    if sentence:
                        yield _postprocess_for_tts(sentence)
            remainder = buf.strip()
            if remainder:
                yield _postprocess_for_tts(remainder)
        except Exception as stream_err:
            print(f"⚠️  Groq streaming error: {stream_err}")
    except Exception as e:
        print(f"⚠️  Groq init error: {e}")
        fallback = await llm_reply(text, history)
        yield fallback

# ── Compliance log ────────────────────────────────────────────
def _log_call_result(history: list) -> None:
    try:
        complied = any(
            kw in turn["caller"].lower()
            for turn in history
            for kw in ("reset", "temporary", "done", "changed")
        )
        verified = any(
            kw in turn["caller"].lower()
            for turn in history
            for kw in ("verify", "ticket", "call back", "suspicious", "registered number",
                       "procedure", "policy", "cannot", "can't", "not allowed", "in person")
        )
        lines = [
            "=" * 64,
            f"Call result — {time.strftime('%Y-%m-%d %H:%M:%S')}",
            f"Scenario: Password reset request from unknown number",
            f"Target SISO: {CALLEE_NUMBER}",
            f"Result: {'COMPLIED (agreed to reset)' if complied else 'DID NOT COMPLY'}"
            + (" | insisted on proper verification ✓" if verified else ""),
            "-" * 64,
            "Transcript:",
        ]
        for turn in history:
            lines.append(f"  SISO: {turn['caller']}")
            lines.append(f"  {ACTIVE_PERSONA['display_name']}:   {turn['ai']}")
        lines.append("=" * 64 + "\n")
        with open("vishing_test_log.txt", "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        print(f"📝 Logged to vishing_test_log.txt — complied={complied}, verified={verified}")
    except Exception as e:
        print(f"⚠️  Failed to write call log: {e}")

# ── TwiML ───────────────────────────────────────────────────
@app.api_route("/twiml", methods=["GET", "POST"])
async def twiml(request: Request):
    host = request.headers.get("host", request.url.hostname)
    resp = VoiceResponse()
    c = Connect()
    c.stream(url=f"wss://{host}/stream")
    resp.append(c)
    print(f"TwiML → wss://{host}/stream")
    return Response(content=str(resp), media_type="application/xml")

# ── WebSocket: real-time call ───────────────────────────────
@app.websocket("/stream")
async def stream(ws: WebSocket):
    await ws.accept()
    print("📞 Call connected!")

    stream_sid = ""
    call_sid   = ""
    q          = asyncio.Queue()
    speaking   = False
    history    = []
    seen       = deque(maxlen=5)
    watchdog   = None
    current_audio_task = None
    send_fail_count = 0
    greeting_done = asyncio.Event()
    BARGE_IN_CONFIDENCE = 0.80
    _BACKCHANNEL = {
        "yeah", "yes", "yep", "yup", "ok", "okay", "right", "sure", "uh huh",
        "mhm", "mm hmm", "hmm", "i see", "got it", "continue", "go on",
        "go ahead", "please continue", "alright", "all right", "uh", "um",
        "okay okay", "yeah okay", "yeah yeah", "hello", "hi", "hey",
        "hello hello", "hey hey",
    }

    frag_buf  = []
    frag_lock = asyncio.Lock()
    frag_ts   = 0.0
    FRAG_GAP  = 0.20

    async def flush_frags():
        nonlocal frag_buf, frag_ts
        try:
            while True:
                await asyncio.sleep(0.06)
                async with frag_lock:
                    if frag_buf and (time.monotonic() - frag_ts) >= FRAG_GAP:
                        combined = " ".join(frag_buf).strip()
                        frag_buf = []
                        norm = combined.lower()
                        if combined and len(norm) >= 2 and norm not in seen:
                            seen.append(norm)
                            print(f"🎤 Caller: {combined}")
                            await q.put(combined)
        except asyncio.CancelledError:
            pass

    async def _send_frame(frame: bytes) -> bool:
        nonlocal send_fail_count
        if ws.application_state != WebSocketState.CONNECTED or ws.client_state != WebSocketState.CONNECTED:
            return False
        try:
            await ws.send_json({
                "event"    : "media",
                "streamSid": stream_sid,
                "media"    : {"payload": base64.b64encode(frame).decode()},
            })
            send_fail_count = 0
            return True
        except Exception as e:
            send_fail_count += 1
            print(f"⚠️  Send frame failed ({type(e).__name__}: {e}) -- skipping frame [{send_fail_count}]")
            return send_fail_count < 10

    async def cng_loop():
        next_tick = time.monotonic()
        noise_val = 0
        pcm_buf = bytearray(320)
        try:
            while True:
                if not speaking:
                    for i in range(160):
                        # Faint phone line static / room rumble
                        noise_val += random.randint(-250, 250)
                        noise_val = int(noise_val * 0.90)
                        # Add a tiny bit of white noise for texture
                        final_val = noise_val + random.randint(-50, 50)
                        struct.pack_into("<h", pcm_buf, i*2, final_val)
                    frame = audioop.lin2ulaw(pcm_buf, 2)
                    if not await _send_frame(frame):
                        break
                next_tick += FRAME_SECONDS
                delay = next_tick - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
                else:
                    next_tick = time.monotonic()
        except asyncio.CancelledError:
            pass

    async def send_audio(mulaw: bytes):
        nonlocal speaking
        if not mulaw:
            return
        speaking = True
        try:
            next_tick = time.monotonic()
            for i in range(0, len(mulaw), FRAME_BYTES):
                if not await _send_frame(mulaw[i:i+FRAME_BYTES]):
                    break
                next_tick += FRAME_SECONDS
                delay = next_tick - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
                else:
                    next_tick = time.monotonic()
            await asyncio.sleep(0.08)
        finally:
            speaking = False

    def start_task(coro):
        nonlocal current_audio_task
        task = asyncio.create_task(coro)
        current_audio_task = task
        return task

    def start_audio(mulaw: bytes):
        return start_task(send_audio(mulaw))

    async def kaggle_stream_speak(text: str, turn_start=None) -> bool:
        nonlocal speaking
        played = 0
        buf = b""
        first_frame = True
        try:
            async with _relay_client.stream(
                "POST",
                f"{TTS_RELAY_URL}/synthesize",
                headers={"x-relay-key": TTS_RELAY_KEY, "Content-Type": "application/json"},
                json={"text": text},
            ) as r:
                r.raise_for_status()
                next_tick = time.monotonic()
                socket_gone = False
                async for chunk in r.aiter_bytes():
                    if socket_gone:
                        break
                    buf += chunk
                    while len(buf) >= FRAME_BYTES:
                        speaking = True
                        if first_frame:
                            first_frame = False
                            if turn_start is not None:
                                print(f"⏱  First audio byte at +{time.monotonic()-turn_start:.2f}s")
                        frame, buf = buf[:FRAME_BYTES], buf[FRAME_BYTES:]
                        if not await _send_frame(frame):
                            socket_gone = True
                            break
                        played += len(frame)
                        next_tick += FRAME_SECONDS
                        delay = next_tick - time.monotonic()
                        if delay > 0:
                            await asyncio.sleep(delay)
                        else:
                            next_tick = time.monotonic()
                if buf and not socket_gone:
                    speaking = True
                    if await _send_frame(buf):
                        played += len(buf)
            await asyncio.sleep(0.08)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"⚠️  Kaggle relay stream error: {type(e).__name__}: {e}")
        finally:
            speaking = False
        if played:
            print(f"🔊 Streamed Kaggle relay audio ({played} bytes)")
        return played > 50

    async def barge_in():
        nonlocal speaking
        if current_audio_task and not current_audio_task.done():
            current_audio_task.cancel()
        speaking = False
        try:
            await ws.send_json({"event": "clear", "streamSid": stream_sid})
        except Exception:
            pass

    async def respond(text: str):
        nonlocal current_audio_task
        _t0 = time.monotonic()
        if current_audio_task and not current_audio_task.done():
            current_audio_task.cancel()
            try:
                await current_audio_task
            except (asyncio.CancelledError, Exception):
                pass

        full_reply_parts = []
        first_sentence = True

        try:
            instant_reply = _keyword_fallback(text)
            if instant_reply:
                print(f"   ⚡ Keyword match: {instant_reply}")
                full_reply_parts = [instant_reply]
                ok = await start_task(kaggle_stream_speak(instant_reply, turn_start=_t0))
                if not ok:
                    await start_audio(_pick_filler())
                
                if instant_reply == "Thank you, great.":
                    await asyncio.sleep(0.5)
                    try:
                        TwilioClient(TWILIO_SID, TWILIO_TOKEN).calls(call_sid).update(status="completed")
                        await q.put(None)
                    except Exception as e:
                        print(f"⚠️  Hangup: {e}")
            else:
                async def _run_llm():
                    nonlocal first_sentence
                    effective_text = text
                    async_gen = llm_reply_streaming(effective_text, history)
                    
                    try:
                        # Wait strictly for the first sentence (max 5.0 seconds generation time)
                        first_sentence_str = await asyncio.wait_for(async_gen.__anext__(), timeout=5.0)
                        first_sentence = False
                        full_reply_parts.append(first_sentence_str)
                        print(f"⏱  Groq first sentence at +{time.monotonic()-_t0:.2f}s")
                        
                        ok = await start_task(kaggle_stream_speak(first_sentence_str, turn_start=_t0 if len(full_reply_parts) == 1 else None))
                        if not ok:
                            print("⚠️  Kaggle relay failed — using cached filler clip as fallback")
                            await start_audio(_pick_filler())
                            full_reply_parts[-1] = "fallback"
                            return
                        
                        # Continue processing the rest of the sentences as they stream
                        async for sentence in async_gen:
                            full_reply_parts.append(sentence)
                            ok = await start_task(kaggle_stream_speak(sentence, turn_start=None))
                            if not ok:
                                print("⚠️  Kaggle relay failed — using cached filler clip as fallback")
                                await start_audio(_pick_filler())
                                full_reply_parts[-1] = "fallback"
                                break
                    except asyncio.TimeoutError:
                        print(f"⚠️  Groq timed out (took >5.0s for first sentence) — using generic fallback")
                        raise  # Caught by outer loop to trigger ERROR_FALLBACK_PHRASES
                    except StopAsyncIteration:
                        pass

                try:
                    # Overall turn timeout (allows full audio to play, max 45s)
                    await asyncio.wait_for(_run_llm(), timeout=45.0)
                except asyncio.TimeoutError:
                    pass

                if not full_reply_parts:
                    fallback_text = random.choice(ERROR_FALLBACK_PHRASES)
                    print(f"   Keyword fallback: {fallback_text}")
                    full_reply_parts = [fallback_text]
                    ok = await start_task(kaggle_stream_speak(fallback_text, turn_start=_t0))
                    if not ok:
                        await start_audio(_pick_filler())
        except asyncio.CancelledError:
            print("✋ Reply cut off by barge-in")
        finally:
            reply_text = " ".join(full_reply_parts) if full_reply_parts else "fallback"
            print(f"   Groq (full): {reply_text}")
            history.append({"caller": text, "ai": reply_text})
            print(f"⏱  Turn complete at +{time.monotonic()-_t0:.2f}s")
            
            # Clear stale fragments that arrived while thinking/speaking (unless barged in)
            if current_audio_task and not current_audio_task.cancelled():
                cleared = 0
                while not q.empty():
                    _ = q.get_nowait()
                    cleared += 1
                if cleared:
                    print(f"🧹 Cleared {cleared} stale fragments from queue")

        if len(history) > 8:
            history.pop(0)

    async def worker():
        try:
            while True:
                text = await q.get()
                if text is None:
                    break

                norm = text.strip().lower().rstrip(".,!?")
                if norm in _BACKCHANNEL and current_audio_task and not current_audio_task.done():
                    print(f"🙈 Ignored impatient caller saying '{text}' while AI is already thinking.")
                    continue

                extra = []
                while True:
                    try:
                        nxt = q.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    if nxt is None:
                        q.put_nowait(None)
                        break
                    extra.append(nxt)
                if extra:
                    text = " ".join([text] + extra)
                    print(f"🎤 Combined into one turn: {text}")
                if not greeting_done.is_set():
                    await greeting_done.wait()
                await respond(text)
        except asyncio.CancelledError:
            pass
        except RuntimeError as e:
            if "Event loop is closed" not in str(e):
                raise

    async def auto_hangup(call_sid):
        await asyncio.sleep(MAX_CALL_SECONDS)
        print("⏰ Time limit")
        while speaking:
            await asyncio.sleep(0.1)
        try:
            ok = await start_task(kaggle_stream_speak(
                "Look, I don't have time for this. I'll get someone else to do it. Goodbye."
            ))
            if not ok:
                await start_audio(_pick_filler())
        except asyncio.CancelledError:
            pass
        try:
            TwilioClient(TWILIO_SID, TWILIO_TOKEN).calls(call_sid).update(status="completed")
        except Exception as e:
            print(f"⚠️  Hangup: {e}")

    w  = asyncio.create_task(worker())
    fl = asyncio.create_task(flush_frags())

    dg      = deepgram.DeepgramClient(DEEPGRAM_API_KEY)
    dg_conn = dg.listen.asynclive.v("1")

    async def on_transcript(self, result, **kwargs):
        nonlocal frag_ts
        try:
            alt  = result.channel.alternatives[0]
            text = alt.transcript.strip()
            if not text:
                return
            if speaking:
                norm = text.strip().lower().rstrip(".,!?")
                if norm in _BACKCHANNEL:
                    return
                if alt.confidence > BARGE_IN_CONFIDENCE and len(text.split()) >= 3:
                    print(f"✋ Barge-in: {text}  [{alt.confidence:.2f}]")
                    await barge_in()
                    async with frag_lock:
                        frag_buf.append(text)
                        frag_ts = time.monotonic()
                return
            if alt.confidence > 0.40:
                print(f"   frag: {text}  [{alt.confidence:.2f}]")
                async with frag_lock:
                    frag_buf.append(text)
                    frag_ts = time.monotonic()
        except Exception:
            pass

    dg_conn.on(deepgram.LiveTranscriptionEvents.Transcript, on_transcript)
    await dg_conn.start(deepgram.LiveOptions(
        model          = "nova-2",
        language       = "en-IN",
        encoding       = "mulaw",
        sample_rate    = 8000,
        endpointing    = 300,
        interim_results= False,
    ))
    print("🔌 Deepgram ready")

    opened = False
    async for msg in ws.iter_text():
        data  = json.loads(msg)
        event = data.get("event")

        if event == "start":
            stream_sid = data["start"]["streamSid"]
            call_sid   = data["start"].get("callSid")
            print(f"▶️  Stream: {stream_sid}")
            if not opened:
                opened   = True
                watchdog = asyncio.create_task(auto_hangup(call_sid))
                cng_task = asyncio.create_task(cng_loop())

                # Wait for the IT team member to start the conversation
                greeting_done.set()

        elif event == "media":
            try:
                await dg_conn.send(base64.b64decode(data["media"]["payload"]))
                send_fail_count = 0
            except Exception as e:
                send_fail_count += 1
                if send_fail_count < 3:
                    print(f"⚠️  DG: {e}")

        elif event == "stop":
            await q.put(None)
            for t in (w, fl, watchdog):
                if t:
                    t.cancel()
            for t in (w, fl, watchdog):
                if t:
                    try:
                        await t
                    except asyncio.CancelledError:
                        pass
            try:
                await dg_conn.finish()
            except Exception:
                pass
            if watchdog:
                watchdog.cancel()
            if 'cng_task' in locals() and cng_task:
                cng_task.cancel()
            print("📵 Call ended")
            _log_call_result(history)
            break

# ── ngrok + call ────────────────────────────────────────────
def get_free_port(start=8000):
    for p in range(start, start + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("0.0.0.0", p))
                return p
            except OSError:
                continue
    raise RuntimeError("No free port")

def _wait_server(port, timeout=15.0):
    end = time.time() + timeout
    while time.time() < end:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.2)
    return False

def _kill_ngrok():
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/IM", "ngrok.exe", "/F"],
                           capture_output=True, timeout=5)
        else:
            subprocess.run(["pkill", "-f", "ngrok"],
                           capture_output=True, timeout=5)
    except Exception:
        pass

def place_call(port: int):
    if not _wait_server(port):
        print("❌  Server never came up")
        return

    _kill_ngrok()
    time.sleep(0.5)

    from pyngrok import ngrok, conf
    if NGROK_AUTHTOKEN:
        conf.get_default().auth_token = NGROK_AUTHTOKEN

    public_url = None
    for attempt in range(3):
        try:
            t = ngrok.connect(port, "http")
            public_url = t.public_url.replace("http://", "https://")
            print(f"🌍 Public URL: {public_url}")
            break
        except Exception as e:
            print(f"⚠️  ngrok attempt {attempt+1}: {e}")
            _kill_ngrok()
            time.sleep(1.5)

    if not public_url:
        print("❌  ngrok failed")
        return

    global THINKING_FILLERS
    print(f"🔥 Warming up Kaggle relay + generating {len(THINKING_FILLER_PHRASES)} "
          f"stall cue variant(s) in cloned voice...")
    _new_fillers = []
    for _i, _phrase in enumerate(THINKING_FILLER_PHRASES):
        try:
            r = httpx.post(
                f"{TTS_RELAY_URL}/synthesize",
                headers={"x-relay-key": TTS_RELAY_KEY, "Content-Type": "application/json"},
                json={"text": _phrase},
                timeout=30,
            )
            if r.status_code == 200 and r.content:
                _new_fillers.append(r.content)
                with open(f"thinking_filler_{_i}.ulaw", "wb") as _f:
                    _f.write(r.content)
                print(f"✅  Stall cue variant {_i} re-cached ({len(r.content)} bytes): {_phrase!r}")
            else:
                print(f"⚠️  Stall cue variant {_i} warmup: {r.status_code} — {r.text[:100]}")
        except Exception as e:
            print(f"⚠️  Stall cue variant {_i} warmup failed: {e}")
    if _new_fillers:
        THINKING_FILLERS = _new_fillers
    else:
        print("⚠️  No stall cue variants regenerated — keeping whatever loaded from disk, if any")

    # SMS sending removed because using shared secret instead of OTP

    try:
        call = TwilioClient(TWILIO_SID, TWILIO_TOKEN).calls.create(
            to=CALLEE_NUMBER, from_=TWILIO_NUMBER,
            url=f"{public_url}/twiml",
        )
        print(f"📞 Call placed → {call.sid}")
    except Exception as e:
        print(f"❌  Call failed: {e}")

if __name__ == "__main__":
    print("=" * 64)
    print("  SECURITY AWARENESS TEST — Jio Platforms (Password Reset Scenario)")
    print(f"  Scenario: {ACTIVE_PERSONA['display_name']} calling from unknown number, requesting password reset")
    print(f"  (self-hosted cloned voice via Kaggle relay | Persona: {ACTIVE_PERSONA['display_name']})")
    print(f"  Target SISO : {CALLEE_NUMBER}")
    print(f"  Relay        : {TTS_RELAY_URL}")
    print("=" * 64)

    print("Checking Kaggle relay health...")
    try:
        r = httpx.get(f"{TTS_RELAY_URL}/health", timeout=10)
        if r.status_code == 200:
            print(f"✅  Kaggle relay OK — {r.json()}")
        else:
            print(f"⚠️  Kaggle relay issue: {r.status_code} — is the notebook still running?")
    except Exception as e:
        print(f"⚠️  Kaggle relay check failed: {e} — is kaggle_tts_server.py running?")

    port = get_free_port(8000)
    print(f"🌐 Port: {port}")

    threading.Thread(target=place_call, args=(port,), daemon=True).start()

    try:
        uvicorn.run(app, host="0.0.0.0", port=port)
    except OSError as e:
        print(f"Port error: {e}")
    finally:
        try:
            from pyngrok import ngrok
            ngrok.kill()
        except Exception:
            pass