# Automates the manual steps in kaggle_tts_server.py's own docstring:
# "open a Kaggle notebook, paste this file in, run it, copy the printed
# ngrok URL into .env by hand." Instead: push the kernel via the Kaggle
# API, wait for it to start, then find its ngrok URL via ngrok's own Cloud
# API (same NGROK_API_KEY already hardcoded in kaggle_tts_server.py) --
# no need to watch the Kaggle notebook page at all.
#
# Usage: python launch_kaggle_relay.py
import os, subprocess, sys, time
import httpx

KERNEL_REF = "smitchavan/tts-relay-server"
# Must match NGROK_API_KEY in kaggle_tts_server.py.
NGROK_API_KEY = "3GREYbPeHIDT4uLEUBdYDLqGWWz_3wYmxf9rieUXnZ3xQzyQk"
ENV_PATH = ".env"
POLL_SECONDS = 10
KERNEL_START_TIMEOUT = 900   # GPU queueing can take a few minutes
TUNNEL_TIMEOUT = 1500        # pip installs + torch reinstall + model load + warmup

# kaggle_tts_server.py has emoji in it (warmup/warning prints). On Windows the
# kaggle CLI otherwise opens files with the system codepage (cp1252) instead
# of UTF-8 and chokes on them ('charmap' codec can't decode byte ...).
# PYTHONUTF8=1 forces UTF-8 for all of Python's default text-mode file opens.
_ENV = {**os.environ, "PYTHONUTF8": "1", "KAGGLE_API_TOKEN": "KGAT_557a4b093e10ece2ecb3465a05fa3659"}


def delete_stale_kernel():
    # Pushing a new version does NOT preempt an already-running session --
    # confirmed by testing: a stale session from an earlier push kept
    # serving (and kept its ngrok tunnel alive) through two subsequent
    # pushes, silently masking that the new code was never actually
    # running. Deleting first guarantees the next push starts genuinely
    # fresh. Failure here just means there was nothing to delete yet.
    subprocess.run(
        ["kaggle", "kernels", "delete", KERNEL_REF, "-y"],
        capture_output=True, text=True, env=_ENV,
    )
    time.sleep(5)  # give the old ngrok tunnel session a moment to drop


def push_kernel():
    delete_stale_kernel()
    print("Pushing kaggle_tts_server.py to Kaggle...")
    result = subprocess.run(
        ["kaggle", "kernels", "push", "-p", "."],
        capture_output=True, text=True, env=_ENV,
    )
    print(result.stdout.strip())
    if result.returncode != 0:
        print(result.stderr.strip())
        raise SystemExit("kaggle kernels push failed -- see output above")


def wait_for_running():
    print("Waiting for the kernel to leave the GPU queue and start running...")
    deadline = time.monotonic() + KERNEL_START_TIMEOUT
    last = ""
    while time.monotonic() < deadline:
        result = subprocess.run(
            ["kaggle", "kernels", "status", KERNEL_REF],
            capture_output=True, text=True, env=_ENV,
        )
        out = (result.stdout or "").strip() or (result.stderr or "").strip()
        if out != last:
            print(f"  {out}")
            last = out
        upper = out.upper()
        if "RUNNING" in upper:
            return
        if "ERROR" in upper or "CANCELACKNOWLEDGED" in upper:
            raise SystemExit(f"Kernel did not start cleanly: {out}")
        time.sleep(POLL_SECONDS)
    raise SystemExit("Timed out waiting for the kernel to start running")


def find_ngrok_url():
    print("Waiting for the ngrok tunnel to come online (model load + warmup happen first)...")
    headers = {"Authorization": f"Bearer {NGROK_API_KEY}", "Ngrok-Version": "2"}
    deadline = time.monotonic() + TUNNEL_TIMEOUT
    while time.monotonic() < deadline:
        try:
            r = httpx.get("https://api.ngrok.com/tunnels", headers=headers, timeout=15)
            r.raise_for_status()
            tunnels = r.json().get("tunnels", [])
            https_urls = [
                t.get("public_url") or t.get("url")
                for t in tunnels
                if (t.get("public_url") or t.get("url", "")).startswith("https://")
            ]
            if https_urls:
                url = https_urls[0]
                print(f"Found tunnel: {url}")
                return url
        except Exception as e:
            print(f"  (not up yet: {e})")
        time.sleep(POLL_SECONDS)
    raise SystemExit(
        "Timed out waiting for the ngrok tunnel. Check the kernel's log on "
        f"https://www.kaggle.com/code/{KERNEL_REF} for what's actually happening."
    )


def update_env(url):
    try:
        with open(ENV_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except FileNotFoundError:
        lines = []
    found = False
    for i, line in enumerate(lines):
        if line.strip().startswith("TTS_RELAY_URL="):
            lines[i] = f"TTS_RELAY_URL={url}\n"
            found = True
    if not found:
        lines.append(f"TTS_RELAY_URL={url}\n")
    with open(ENV_PATH, "w", encoding="utf-8") as f:
        f.writelines(lines)
    print(f"Updated {ENV_PATH}: TTS_RELAY_URL={url}")


if __name__ == "__main__":
    push_kernel()
    wait_for_running()
    url = find_ngrok_url()
    update_env(url)
    print("\nReady. Run: python ai_voice_demo_kaggle.py")