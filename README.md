# Telegram Bridge for little-coder

Telegram bridge to [little-coder](https://github.com/itayinbarr/little-coder), using its own `PiRpc` client (`benchmarks/rpc_client.py`).

This does **not** reimplement pi's RPC wire protocol — it drives the actual `PiRpc` class from the little-coder repo, so you get the exact same extensions, `AGENTS.md` system prompt, and speed as running `little-coder` in a terminal. Telegram just replaces the TUI as the front end.

## Setup

1.  **Clone + build little-coder.**
    
    `rpc_client.py` hardcodes `node_modules/.bin/pi`, so it needs the **local build**, not the global `npm install -g little-coder`:
    
    ```bash
    git clone https://github.com/itayinbarr/little-coder.git
    cd little-coder && npm install
    ```
    
2.  **Install this bot's one dependency.** There are a few options:
    
    ```bash
    # Option 1: virtual environment (safest, most standard)
    python3 -m venv venv
    source venv/bin/activate
    pip install python-telegram-bot
    
    # Option 2: pipx (good for CLI tools/bots)
    pipx install python-telegram-bot
    
    # Option 3: --user flag (installs to your user site-packages, not system-wide)
    pip install python-telegram-bot --user
    ```
    
3.  **Drop this file** at `<little-coder-clone>/benchmarks/telegram_bridge.py` so `from rpc_client import PiRpc` resolves. (Or edit the `sys.path` line at the top of the script to point at wherever `benchmarks/`lives.)
    
4.  **Set environment variables:**
    
    ```bash
    export TELEGRAM_BOT_TOKEN=123456:ABC-your-bot-token
    export TELEGRAM_ALLOWED_USER_IDS=123456789          # comma-separated Telegram user IDs
    export LLAMACPP_API_KEY=noop
    export LLAMACPP_BASE_URL=http://127.0.0.1:8091/v1   # your llama-server port
    export LC_MODEL=llamacpp/qwen3.6-35b-a3b
    export LC_PROJECT_ROOT=/home/username/code             # base dir chats can cd into
    
    # Optional — see security note below:
    export LITTLE_CODER_PERMISSION_MODE=accept-all
    export LITTLE_CODER_BASH_ALLOW="make ,docker compose ps"
    ```
    
5.  **Run:**
    
    ```bash
    python3 <little-coder-clone>/benchmarks/telegram_bridge.py
    ```
    

## Security

This gives Telegram-side users Bash/Read/Write/Edit access on whatever directory the chat's session is pointed at.

-   `TELEGRAM_ALLOWED_USER_IDS` is a hard allowlist checked on every message — keep it to your own Telegram user ID.
-   Sessions are confined under `LC_PROJECT_ROOT` (no `..` escapes), so a compromised or misbehaving session can't wander outside your projects directory.
-   `rm` / `sudo` stay off the bash whitelist unless you add them yourself via `LITTLE_CODER_BASH_ALLOW` — do that deliberately, not by default.