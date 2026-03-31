#!/usr/bin/env python3
"""Last30Days Research Dashboard — lightweight server.

Bridges the web UI to last30days.py.
Run:  python3 ui/server.py [--port 8030]
Open: http://localhost:8030
"""

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "last30days.py"
HISTORY_FILE = ROOT / "ui" / ".research_history.json"

# In-memory store for running/completed jobs
_jobs: dict = {}
_job_counter = 0
_lock = threading.Lock()


def _load_history() -> list:
    if HISTORY_FILE.exists():
        try:
            return json.loads(HISTORY_FILE.read_text())
        except Exception:
            return []
    return []


def _save_history(history: list):
    HISTORY_FILE.write_text(json.dumps(history[-200:], indent=2))


def _run_research(job_id: str, topic: str, opts: dict):
    """Execute last30days.py in a subprocess."""
    cmd = [sys.executable, str(SCRIPT), topic, "--emit=json"]

    depth = opts.get("depth", "default")
    if depth == "quick":
        cmd.append("--quick")
    elif depth == "deep":
        cmd.append("--deep")

    days = opts.get("days")
    if days:
        cmd.extend(["--days", str(days)])

    sources = opts.get("sources")
    if sources:
        cmd.extend(["--search", sources])

    x_handle = opts.get("x_handle")
    if x_handle:
        cmd.extend(["--x-handle", x_handle])

    if opts.get("include_web"):
        cmd.append("--include-web")

    if opts.get("store"):
        cmd.append("--store")

    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"

    try:
        with _lock:
            _jobs[job_id]["status"] = "running"
            _jobs[job_id]["cmd"] = " ".join(cmd)

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=360,
            cwd=str(ROOT),
            env=env,
        )

        output = result.stdout
        # Try to parse JSON from the output
        json_data = None
        try:
            json_data = json.loads(output)
        except Exception:
            # Maybe the JSON is embedded in other output
            for line in output.split("\n"):
                line = line.strip()
                if line.startswith("{"):
                    try:
                        json_data = json.loads(line)
                        break
                    except Exception:
                        pass

        with _lock:
            _jobs[job_id]["status"] = "done"
            _jobs[job_id]["result"] = json_data
            _jobs[job_id]["raw_output"] = output[:50000]
            _jobs[job_id]["stderr"] = result.stderr[:5000] if result.stderr else ""
            _jobs[job_id]["returncode"] = result.returncode
            _jobs[job_id]["finished_at"] = time.time()

        # Save to history
        entry = {
            "id": job_id,
            "topic": topic,
            "opts": opts,
            "started_at": _jobs[job_id]["started_at"],
            "finished_at": _jobs[job_id]["finished_at"],
            "returncode": result.returncode,
            "item_count": len(json_data.get("items", [])) if json_data else 0,
        }
        history = _load_history()
        history.append(entry)
        _save_history(history)

    except subprocess.TimeoutExpired:
        with _lock:
            _jobs[job_id]["status"] = "timeout"
            _jobs[job_id]["finished_at"] = time.time()
    except Exception as e:
        with _lock:
            _jobs[job_id]["status"] = "error"
            _jobs[job_id]["error"] = str(e)
            _jobs[job_id]["finished_at"] = time.time()


class DashboardHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT / "ui"), **kwargs)

    def log_message(self, fmt, *args):
        # Quiet logging
        pass

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/jobs":
            self._json_response(list(_jobs.values()))
        elif path.startswith("/api/jobs/"):
            job_id = path.split("/")[-1]
            job = _jobs.get(job_id)
            if job:
                self._json_response(job)
            else:
                self._json_response({"error": "not found"}, 404)
        elif path == "/api/history":
            self._json_response(_load_history())
        elif path == "/api/diagnose":
            self._run_diagnose()
        elif path == "/api/presets":
            self._json_response(PRESETS)
        else:
            if path == "/":
                self.path = "/index.html"
            super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)

        if parsed.path == "/api/research":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}

            topic = body.get("topic", "").strip()
            if not topic:
                self._json_response({"error": "topic required"}, 400)
                return

            global _job_counter
            with _lock:
                _job_counter += 1
                job_id = f"job-{_job_counter}"
                _jobs[job_id] = {
                    "id": job_id,
                    "topic": topic,
                    "opts": body.get("opts", {}),
                    "status": "queued",
                    "started_at": time.time(),
                    "result": None,
                }

            t = threading.Thread(target=_run_research, args=(job_id, topic, body.get("opts", {})), daemon=True)
            t.start()

            self._json_response({"job_id": job_id, "status": "queued"})

        elif parsed.path == "/api/cancel":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
            job_id = body.get("job_id")
            if job_id and job_id in _jobs:
                _jobs[job_id]["status"] = "cancelled"
                self._json_response({"ok": True})
            else:
                self._json_response({"error": "not found"}, 404)
        else:
            self._json_response({"error": "not found"}, 404)

    def _json_response(self, data, code=200):
        body = json.dumps(data, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _run_diagnose(self):
        try:
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "--diagnose"],
                capture_output=True, text=True, timeout=30, cwd=str(ROOT),
            )
            self._json_response({"output": result.stdout, "stderr": result.stderr})
        except Exception as e:
            self._json_response({"error": str(e)}, 500)


PRESETS = [
    {
        "id": "competitive-intel",
        "name": "Competitive Intelligence",
        "icon": "🎯",
        "description": "Monitor competitors across all platforms — track sentiment shifts over time",
        "template": "{competitor_name} reviews complaints",
        "opts": {"depth": "deep", "days": 30, "sources": "reddit,x,hn,youtube,web", "store": True},
    },
    {
        "id": "trend-radar",
        "name": "Trend Radar",
        "icon": "📡",
        "description": "Spot emerging trends before they peak — volume & engagement breakout detection",
        "template": "{topic} trend emerging",
        "opts": {"depth": "deep", "days": 7, "sources": "reddit,x,hn,youtube,tiktok", "store": True},
    },
    {
        "id": "content-calendar",
        "name": "Content Calendar",
        "icon": "📅",
        "description": "Research trending content in your niche to generate a week of content ideas",
        "template": "{niche} best content viral",
        "opts": {"depth": "default", "days": 14, "sources": "reddit,x,youtube,tiktok,instagram"},
    },
    {
        "id": "crisis-detect",
        "name": "Crisis Detection",
        "icon": "🚨",
        "description": "Quick-scan for negative sentiment spikes on your brand or product",
        "template": "{brand} problem issue outage",
        "opts": {"depth": "quick", "days": 1, "sources": "reddit,x,hn,web"},
    },
    {
        "id": "investment-signal",
        "name": "Investment Signals",
        "icon": "📈",
        "description": "Cross-reference social buzz with Polymarket predictions for alpha",
        "template": "{asset_or_topic} prediction market sentiment",
        "opts": {"depth": "deep", "days": 7, "sources": "reddit,x,polymarket,hn,web", "store": True},
    },
    {
        "id": "launch-tracker",
        "name": "Product Launch Tracker",
        "icon": "🚀",
        "description": "Track a product launch across all channels — real reception vs marketing",
        "template": "{product} launch release review first impressions",
        "opts": {"depth": "deep", "days": 7, "sources": "reddit,x,youtube,hn,web", "store": True},
    },
    {
        "id": "audience-research",
        "name": "Audience Research Lab",
        "icon": "🔬",
        "description": "Deep-dive what your target audience actually says — pain points & language",
        "template": "{audience} frustration wish recommendation",
        "opts": {"depth": "deep", "days": 30, "sources": "reddit,x,youtube"},
    },
    {
        "id": "viral-reverse",
        "name": "Viral Content Analyzer",
        "icon": "⚡",
        "description": "Find top-engagement content patterns — reverse-engineer what drives shares",
        "template": "{topic} viral trending popular",
        "opts": {"depth": "deep", "days": 14, "sources": "reddit,x,youtube,tiktok,instagram"},
    },
    {
        "id": "influencer-discovery",
        "name": "Expert & Influencer Discovery",
        "icon": "🔍",
        "description": "Find who drives conversations — X handles, subreddits, YouTube creators",
        "template": "{topic} expert opinion analysis",
        "opts": {"depth": "deep", "days": 30, "sources": "reddit,x,youtube,hn"},
    },
    {
        "id": "comparative-analysis",
        "name": "Comparative Market Analysis",
        "icon": "⚖️",
        "description": "Head-to-head comparison across all sources with engagement-weighted scoring",
        "template": "{product_a} vs {product_b}",
        "opts": {"depth": "deep", "days": 30, "sources": "reddit,x,youtube,hn,web", "store": True},
    },
]


def main():
    parser = argparse.ArgumentParser(description="Last30Days Dashboard Server")
    parser.add_argument("--port", type=int, default=8030)
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()

    server = HTTPServer((args.host, args.port), DashboardHandler)
    print(f"🔬 Last30Days Dashboard running at http://localhost:{args.port}")
    print(f"   Press Ctrl+C to stop\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.shutdown()


if __name__ == "__main__":
    main()
