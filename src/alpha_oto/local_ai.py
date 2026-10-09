"""Optional local-only model briefing. No remote URLs or execution tools."""
from __future__ import annotations
import json
from urllib.parse import urlparse
from urllib.request import Request, urlopen


def summarize_locally(report: dict, *, model: str = "qwen2.5:3b", endpoint: str = "http://127.0.0.1:11434") -> str:
    parsed = urlparse(endpoint)
    # Explicitly prohibit cloud use, redirects through custom hosts, etc.
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"} or parsed.port != 11434:
        raise ValueError("Ollama endpoint restricted to local port 11434")
    prompt = ("Summarize the following backtest diagnostic in neutral language. "
              "Explain overfitting, costs, uncertainty and drawdown. Never claim guaranteed profits. "
              "Do not propose or send trades.\n"+json.dumps(report,sort_keys=True)[:12000])
    req = Request("http://127.0.0.1:11434/api/generate",
                  data=json.dumps({"model":model,"prompt":prompt,"stream":False}).encode(),
                  headers={"Content-Type":"application/json"},method="POST")
    # Never called automatically: the user explicitly invokes `local-report`.
    with urlopen(req,timeout=120) as response:
        return str(json.load(response).get("response",""))
