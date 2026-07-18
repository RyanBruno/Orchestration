#!/usr/bin/env python3
"""Stateless dashboard server: stdlib only, no framework.

The defining property of this server is that it holds NO state between
requests. Every GET /api/snapshot walks the actual directories on disk
(state/, heartbeats/, mailboxes/, operator-pending/, gates.json) fresh,
right then, and computes the current picture from what it finds --
including the 2.5x staleness math, using the exact same
orchestrator.common.heartbeat_status() function the orchestrator itself
uses, so the dashboard and the orchestrator can never disagree about what
"stalled" means. Restarting this server has zero effect on correctness,
only on availability -- there is nothing cached to lose.

Run: python3 dashboard/server.py [--port 8765]
Then open http://localhost:8765/ in a browser.
"""

import argparse
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from orchestrator import common, config, gates, tick  # noqa: E402

INDEX_HTML_PATH = Path(__file__).resolve().parent / "index.html"


def _worker_snapshot(worker: str) -> dict:
    state = common.read_worker_state(worker)
    hb = common.heartbeat_status(worker)
    inbox_count = len(common.read_inbox(worker))
    done_dir = config.MAILBOX_DIR / worker / "done"
    done_count = len(list(done_dir.glob("*.json"))) if done_dir.exists() else 0
    return {
        "worker": worker,
        "state": state,
        "heartbeat": hb,
        "inbox_count": inbox_count,
        "done_count": done_count,
    }


def build_snapshot() -> dict:
    """Pure read of current disk state. No caching, no memoization."""
    worker_names = common.list_known_workers()  # excludes "orchestrator" itself
    workers = [_worker_snapshot(w) for w in worker_names]
    orchestrator_snapshot = _worker_snapshot("orchestrator")

    tasks = tick.list_tasks()
    task_counts = {}
    for t in tasks:
        task_counts[t.get("status", "?")] = task_counts.get(t.get("status", "?"), 0) + 1

    return {
        "generated_ts": common.now_iso(),
        "orchestrator": orchestrator_snapshot,
        "workers": workers,
        "tasks": tasks,
        "task_counts": task_counts,
        "heavy_in_flight": sum(1 for t in tasks if t.get("heavy") and t.get("status") in ("dispatched", "in_progress")),
        "max_concurrent_heavy": config.MAX_CONCURRENT_HEAVY,
        "operator_pending": gates.list_pending(),
        "gates_config": gates.load_gates(),
        "staleness_multiplier": config.STALENESS_MULTIPLIER,
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # keep stdout quiet; this is a local dev dashboard, not a service

    def _send_json(self, obj, status=200):
        body = json.dumps(obj, indent=2, sort_keys=True).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            body = INDEX_HTML_PATH.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/api/snapshot":
            self._send_json(build_snapshot())
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        if self.path == "/api/resolve":
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            try:
                req = json.loads(raw or b"{}")
                pending_id = req["pending_id"]
                decision = req["decision"]
                resolved_by = req.get("resolved_by", "dashboard-operator")
                note = req.get("note", "")
                item = gates.resolve_pending(pending_id, decision, resolved_by, note)
                self._send_json({"ok": True, "item": item})
            except (KeyError, ValueError, FileNotFoundError) as e:
                self._send_json({"ok": False, "error": str(e)}, status=400)
            return
        self.send_response(404)
        self.end_headers()


def main():
    parser = argparse.ArgumentParser(description="Stateless orchestration dashboard")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = HTTPServer(("localhost", args.port), Handler)
    print(f"Dashboard on http://localhost:{args.port}/ (base dir: {config.BASE_DIR})")
    server.serve_forever()


if __name__ == "__main__":
    main()
