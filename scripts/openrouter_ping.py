"""Diagnostic: hit OpenRouter directly with httpx to confirm key + connectivity.
Bypasses the openai SDK entirely.
"""
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from conductor.secrets import load_dotenv
load_dotenv(Path.cwd() / ".env")
load_dotenv(Path(__file__).parent.parent / ".env")

import httpx

key = os.environ.get("OPENROUTER_API_KEY")
print("key set:", bool(key), "len:", len(key) if key else 0)
if not key:
    raise SystemExit(1)

model = sys.argv[1] if len(sys.argv) > 1 else "anthropic/claude-haiku-4.5"
print("model:", model)

t0 = time.time()
r = httpx.post(
    "https://openrouter.ai/api/v1/chat/completions",
    headers={
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://tallyhq.org",
        "X-Title": "TallyHQ",
    },
    json={
        "model": model,
        "messages": [{"role": "user", "content": "Say 'ok' in 1 word."}],
        "max_tokens": 10,
    },
    timeout=30.0,
)
print(f"status: {r.status_code} (took {time.time()-t0:.1f}s)")
print("body:", r.text[:1000])
