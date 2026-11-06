# config.example.py
#
# Non-functional configuration stub. This repo intentionally does NOT include
# the live call-trigger logic, the persona/system prompt, or the voice-cloning
# pipeline — see README.md > "What's in This Repo".
#
# Copy to config.py and fill in your own values if you are reproducing this
# kind of exercise under your own authorization and consent controls.

TWILIO_ACCOUNT_SID = ""
TWILIO_AUTH_TOKEN = ""
TWILIO_FROM_NUMBER = ""

DEEPGRAM_API_KEY = ""
LLM_API_KEY = ""          # dialogue generation provider of your choice
TTS_RELAY_URL = ""        # your own voice-cloning inference endpoint

# Hard safety limits used in the original exercise — keep these non-negotiable
# in any reproduction:
MAX_CALL_SECONDS = 180
TARGET_MUST_BE_CONSENTING_INTERNAL_NUMBER = True
NEVER_REQUEST_BEYOND = "a single one-time desk-presence code with no system access"
STOP_IMMEDIATELY_IF_TARGET_SAYS = ["I'll verify independently", "I'm reporting this"]
