# Pandora Tool Server (Raspberry Pi)

A tiny no-code tool host for Pandora. Rye defines tools through a web UI —
shell commands with `{param}` placeholders, or inline Python scripts —
and Pandora calls them over HTTP as if they were her own native functions.

**Stdlib only.** No pip, no venv, no dependencies. Runs on any Pi with Python 3.

## Run it

```bash
cd ~/pandora-toolserver
python3 toolserver.py            # port 8791
python3 toolserver.py --port 9000
```

Then open `http://<pi-ip>:8791` in a browser — that's the management UI:
add tools, test-run them, edit, delete, watch the execution log.

## Auto-start (systemd)

```bash
sudo tee /etc/systemd/system/pandora-toolserver.service > /dev/null <<'EOF'
[Unit]
Description=Pandora tool server
After=network.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/pandora-toolserver
ExecStart=/usr/bin/python3 /home/pi/pandora-toolserver/toolserver.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now pandora-toolserver
```

Check it's alive: `curl http://localhost:8791/api/status`

## Reach it from the Ryebox

The Pi and the Ryebox need to see each other on the LAN (or both on
Tailscale — recommended, same as the phone app).

1. Find the Pi's address: on the Pi, `hostname -I`.
2. On the Ryebox, open Pandora's HUD → settings → **TOOL SERVER**:
   - `toolserver_enabled` → ON
   - `toolserver_ip` → the Pi's address (e.g. `192.168.1.50`)
   - `toolserver_port` → `8791`
   - Hit **TEST CONNECTION** — it should report the tool count.
3. Done. Pandora's next turn includes the Pi's tools as `pi_<name>`
   functions alongside her local tools. She calls them; the Pi runs them;
   results come back into the conversation.

## Defining tools

Two kinds, both created in the web UI — no coding on the Pi required.

**Shell command** — with `{param}` placeholders filled at call time:

| field | example |
|---|---|
| name | `weather` |
| description | `Get current weather for a city` |
| command | `curl -s 'wttr.in/{city}?format=3'` |
| params | `city` = `city name` |
| timeout | `15` |

Pandora calls `pi_weather(city="Nashville")` → the Pi runs
`curl -s 'wttr.in/Nashville?format=3'` → output returns as the tool result.

**Inline Python** — reads `{"args": {...}}` on stdin, prints JSON on stdout:

```python
import json, sys
data = json.load(sys.stdin)
name = data["args"].get("name", "world")
print(json.dumps({"greeting": f"hello {name}"}))
```

## Files

| file | what |
|---|---|
| `toolserver.py` | the server (this is the whole backend) |
| `index.html` | management UI, served at `/` |
| `tools.json` | tool registry (created on first save) |
| `tool_log.jsonl` | execution log, last 100 runs |

## API (for Pandora's client)

- `GET /api/tools` → `{"tools": [{name, description, params, timeout, kind}]}`
- `GET /api/tools/<name>` → full spec including command/script
- `POST /api/tools` → create
- `PUT /api/tools/<name>` → update (merge)
- `DELETE /api/tools/<name>` → remove
- `POST /api/run` → `{"tool": name, "args": {...}}` → `{"ok": true, "output": ...}`
- `GET /api/log?limit=50` → recent runs
- `GET /api/status` → `{"uptime_s", "tool_count", "last_run"}`

## Notes

- Everything the Pi runs is logged with timestamp, args, and duration —
  same accountability deal as Pandora's local tools.
- Keep the Pi on the LAN or Tailscale only. Don't expose 8791 to the
  internet; there's no auth — it's a trusted-network service.

## What else can move to the Pi

Rye's ask: shed load off the Ryebox. The Pi is I/O-bound-friendly,
lightweight, and always-on — perfect for background services. The tool
server is the FIRST Pi service, not the only one.

**Move to Pi (I/O-bound, lightweight, always-on):**
- **Tool server** (this build) — shell, scripts, web fetching
- **SearXNG instance** — self-hosted search for her curiosity research
  (docker or bare metal); research.py points at the Pi instead of localhost
- **Dream orchestration** — the file-gathering, prompt-building,
  log-processing parts of the dream cycle (the LLM calls still hit
  Ollama on the Ryebox over the network)
- **Health watchdog** — monitor the Ryebox's Pandora processes remotely,
  alert on failure (pings the Ryebox API, watches for silence)
- **House pattern analysis** — house.py log crunching; batch, not real-time
- **HUD server** — lightweight HTTP, can serve the UI from the Pi

**Stays on Ryebox (needs GPU, audio hardware, or real-time):**
- **Ollama + all models** — needs the 6900 XT, no contest
- **Listener** — the microphone and speakers are physical; it lives
  where the ears and mouth are
- **Whisper STT, Piper TTS, wake word** — real-time audio; the Pi is
  too slow and has no audio hardware worth using
- **Vision/VLM** — needs the GPU

### Pi service API pattern

Every Pi service follows the same shape so Pandora (and the HUD) can
talk to all of them uniformly:

- `GET /api/health` → `{"ok": true, "service": "<name>", "uptime_s": N}`
- `GET /api/tools` → list capabilities (for tool-like services)
- `POST /api/run` → `{"tool": name, "args": {...}}` → `{"ok", "output"|"error"}`
- `GET /api/log` → recent execution log

The tool server implements all four. Future services implement at
minimum `/api/health` and `/api/log`.

### Pi services calling the Ryebox LLM

Any Pi service that needs LLM work calls the Ryebox's Ollama directly —
the models stay on the 6900 XT, always:

```
http://<ryebox-ip>:11434/api/chat
```

Same `/api/chat` payload Pandora's llm.py sends (model, messages,
optional tools). The Pi never runs a model itself; it's hands and eyes,
not the brain.
