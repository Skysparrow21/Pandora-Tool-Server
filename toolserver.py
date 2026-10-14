#!/usr/bin/env python3
"""toolserver.py — Pandora's Raspberry Pi tool server.

A tiny no-code tool host: Rye defines tools (shell commands with {param}
substitution, or inline Python scripts) through the web UI, and Pandora
calls them over HTTP like native function calls.

Stdlib only — no pip deps. Runs on any Pi with Python 3:
    python3 toolserver.py            # port 8791
    python3 toolserver.py --port 9000

Files (next to this script):
    tools.json       tool registry
    tool_log.jsonl   execution log (last 100 kept)
    index.html       management UI (served at /)

API:
    GET  /api/tools          list tools (name, description, params)
    POST /api/tools          create a tool        {"name","description","command"|"script","params","timeout"}
    PUT  /api/tools/<name>   update a tool
    DELETE /api/tools/<name> remove a tool
    POST /api/run            run a tool           {"tool":"name","args":{...}}
    GET  /api/log            recent execution log
    GET  /api/status         uptime, tool count, last run
    GET  /api/health         standard Pi-service health (ok/service/uptime)

Every execution is logged: timestamp, tool, args, ok/fail, duration.
"""

import http.server
import json
import os
import re
import socketserver
import subprocess
import sys
import threading
import time
import urllib.parse
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOLS_PATH = HERE / "tools.json"
LOG_PATH = HERE / "tool_log.jsonl"
INDEX_PATH = HERE / "index.html"

PORT = 8791
STARTED = time.time()
MAX_LOG = 100

_lock = threading.Lock()


# ---------------- registry ----------------

def load_tools() -> dict:
    try:
        return json.loads(TOOLS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_tools(tools: dict):
    TOOLS_PATH.write_text(json.dumps(tools, indent=2), encoding="utf-8")


# ---------------- log ----------------

def log_run(name, args, ok, duration, output=""):
    entry = {
        "ts": time.time(),
        "tool": name,
        "args": args,
        "ok": ok,
        "duration_s": round(duration, 2),
        "output": str(output)[:500],
    }
    with _lock:
        try:
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
            # trim to last MAX_LOG
            lines = LOG_PATH.read_text(encoding="utf-8").splitlines()
            if len(lines) > MAX_LOG:
                LOG_PATH.write_text("\n".join(lines[-MAX_LOG:]) + "\n",
                                    encoding="utf-8")
        except OSError:
            pass


def read_log(limit=50):
    try:
        lines = LOG_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines[-limit:]:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return out


# ---------------- execution ----------------

_PARAM_RE = re.compile(r"\{(\w+)\}")


def _substitute(template: str, args: dict) -> str:
    """Replace {param} placeholders. Missing params become empty strings."""
    def _rep(m):
        return str(args.get(m.group(1), ""))
    return _PARAM_RE.sub(_rep, template)


def run_tool(name: str, args: dict) -> dict:
    tools = load_tools()
    spec = tools.get(name)
    if spec is None:
        return {"ok": False, "error": f"unknown tool: {name}"}
    args = args or {}
    timeout = int(spec.get("timeout", 15))
    t0 = time.time()
    try:
        if "script" in spec:
            # Inline Python: JSON args on stdin, JSON result on stdout.
            payload = json.dumps({"args": args, "params": args})
            p = subprocess.run(
                [sys.executable, "-c", spec["script"]],
                input=payload, capture_output=True, text=True,
                timeout=timeout, cwd=str(HERE))
            dur = time.time() - t0
            if p.returncode != 0:
                err = (p.stderr or "script failed")[-1000:]
                log_run(name, args, False, dur, err)
                return {"ok": False, "error": err}
            try:
                result = json.loads(p.stdout) if p.stdout.strip() else {}
            except json.JSONDecodeError:
                result = {"raw": p.stdout[-2000:]}
            log_run(name, args, True, dur, str(result)[:200])
            return {"ok": True, "output": result}
        else:
            cmd = _substitute(spec.get("command", ""), args)
            p = subprocess.run(
                cmd, shell=True, capture_output=True, text=True,
                timeout=timeout, cwd=str(HERE))
            dur = time.time() - t0
            out = (p.stdout or "")[-4000:]
            if p.returncode != 0:
                err = ((p.stderr or "") + out)[-1000:] or \
                    f"exit {p.returncode}"
                log_run(name, args, False, dur, err)
                return {"ok": False, "error": err}
            log_run(name, args, True, dur, out[:200])
            return {"ok": True, "output": out}
    except subprocess.TimeoutExpired:
        dur = time.time() - t0
        log_run(name, args, False, dur, "timeout")
        return {"ok": False, "error": f"timed out after {timeout}s"}
    except Exception as e:
        dur = time.time() - t0
        log_run(name, args, False, dur, str(e))
        return {"ok": False, "error": str(e)}


# ---------------- HTTP ----------------

class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "PandoraToolServer/1.0"

    def _json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
        except ValueError:
            n = 0
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        if path == "/":
            try:
                body = INDEX_PATH.read_bytes()
            except OSError:
                body = b"<h1>toolserver</h1><p>index.html missing</p>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/tools":
            tools = load_tools()
            return self._json({"tools": [
                {"name": n,
                 "description": t.get("description", ""),
                 "params": t.get("params", {}),
                 "timeout": t.get("timeout", 15),
                 "kind": "script" if "script" in t else "command"}
                for n, t in tools.items()]})
        if path == "/api/log":
            qs = urllib.parse.parse_qs(parsed.query)
            limit = int(qs.get("limit", ["50"])[0])
            return self._json({"log": read_log(limit)})
        if path == "/api/status":
            tools = load_tools()
            log = read_log(1)
            return self._json({
                "uptime_s": round(time.time() - STARTED, 1),
                "tool_count": len(tools),
                "last_run": log[-1] if log else None,
            })
        if path == "/api/health":
            # Standard Pi-service health shape. Every future Pi service
            # (dream orchestrator, watchdog, searxng proxy, ...) exposes
            # the same endpoint so Pandora can poll them uniformly.
            return self._json({"ok": True, "service": "toolserver",
                               "uptime_s": round(time.time() - STARTED, 1)})
        m = re.match(r"^/api/tools/([a-zA-Z0-9_]+)$", path)
        if m:
            tools = load_tools()
            spec = tools.get(m.group(1))
            if spec is None:
                return self._json({"error": "not found"}, 404)
            full = dict(spec)
            full["name"] = m.group(1)
            full["kind"] = "script" if "script" in spec else "command"
            return self._json({"tool": full})
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        body = self._read_body()
        if path == "/api/run":
            name = body.get("tool", "")
            args = body.get("args", {})
            if not name:
                return self._json({"ok": False, "error": "missing tool name"},
                                  400)
            return self._json(run_tool(name, args))
        if path == "/api/tools":
            name = (body.get("name") or "").strip()
            if not name or not re.match(r"^[a-zA-Z0-9_]+$", name):
                return self._json(
                    {"ok": False,
                     "error": "name required: letters, numbers, underscores"},
                    400)
            if "command" not in body and "script" not in body:
                return self._json(
                    {"ok": False, "error": "need 'command' or 'script'"},
                    400)
            tools = load_tools()
            tools[name] = {
                "description": body.get("description", ""),
                "params": body.get("params", {}) or {},
                "timeout": int(body.get("timeout", 15)),
                **({"command": body["command"]} if "command" in body
                   else {"script": body["script"]}),
            }
            save_tools(tools)
            return self._json({"ok": True, "name": name})
        self._json({"error": "not found"}, 404)

    def do_PUT(self):
        parsed = urllib.parse.urlparse(self.path)
        m = re.match(r"^/api/tools/([a-zA-Z0-9_]+)$",
                     parsed.path.rstrip("/"))
        if not m:
            return self._json({"error": "not found"}, 404)
        name = m.group(1)
        body = self._read_body()
        tools = load_tools()
        if name not in tools:
            return self._json({"ok": False, "error": f"unknown tool: {name}"},
                              404)
        cur = tools[name]
        for k in ("description", "params", "timeout", "command", "script"):
            if k in body and body[k] is not None:
                cur[k] = body[k]
        # switching kinds: drop the other one if both present
        if "command" in body and "script" in cur:
            del cur["script"]
        if "script" in body and "command" in cur:
            del cur["command"]
        tools[name] = cur
        save_tools(tools)
        return self._json({"ok": True, "name": name})

    def do_DELETE(self):
        parsed = urllib.parse.urlparse(self.path)
        m = re.match(r"^/api/tools/([a-zA-Z0-9_]+)$",
                     parsed.path.rstrip("/"))
        if not m:
            return self._json({"error": "not found"}, 404)
        name = m.group(1)
        tools = load_tools()
        if name not in tools:
            return self._json({"ok": False, "error": f"unknown tool: {name}"},
                              404)
        del tools[name]
        save_tools(tools)
        return self._json({"ok": True, "name": name})


class ThreadedServer(socketserver.ThreadingMixIn,
                     http.server.HTTPServer):
    daemon_threads = True


def main():
    port = PORT
    for i, a in enumerate(sys.argv[1:]):
        if a == "--port" and i + 1 < len(sys.argv[1:]):
            port = int(sys.argv[2:][i])
        elif a.startswith("--port="):
            port = int(a.split("=", 1)[1])
    srv = ThreadedServer(("0.0.0.0", port), Handler)
    print(f"Pandora tool server on :{port}  (registry: {TOOLS_PATH})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
