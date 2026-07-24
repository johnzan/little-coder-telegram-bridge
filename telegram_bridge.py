"""
Telegram bridge to little-coder, using its own PiRpc client (benchmarks/rpc_client.py).

This does NOT reimplement pi's RPC wire protocol — it drives the actual PiRpc
class from the little-coder repo, so you get the exact same extensions,
AGENTS.md system prompt, and speed as running `little-coder` in a terminal.
Telegram just replaces the TUI as the front end.

── Setup ──────────────────────────────────────────────────────────────────

1. Clone + build little-coder. rpc_client.py hardcodes
   `node_modules/.bin/pi`, so it needs the LOCAL build, not the global
   `npm install -g little-coder`:

     git clone https://github.com/itayinbarr/little-coder.git
     cd little-coder && npm install

2. Install this bot's one dependency:

     pip install python-telegram-bot --break-system-packages

3. Drop this file at <little-coder-clone>/benchmarks/telegram_bridge.py
   so `from rpc_client import PiRpc` resolves. (Or edit the sys.path
   line below to point at wherever benchmarks/ lives.)

4. Env vars:

     export TELEGRAM_BOT_TOKEN=123456:ABC-your-bot-token
     export TELEGRAM_ALLOWED_USER_IDS=123456789          # comma-separated Telegram user IDs
     export LLAMACPP_API_KEY=noop
     export LLAMACPP_BASE_URL=http://127.0.0.1:8091/v1   # your llama-server port
     export LC_MODEL=llamacpp/qwen3.6-35b-a3b
     export LC_PROJECT_ROOT=/home/username/code           # base dir chats can cd into
     # Optional — see security note below:
     export LITTLE_CODER_PERMISSION_MODE=accept-all
     export LITTLE_CODER_BASH_ALLOW="make ,docker compose ps"

5. Run:  python telegram_bridge.py

── Security ─────────────────────────────────────────────────────────────

This gives Telegram-side users Bash/Read/Write/Edit on whatever directory
the chat's session is pointed at. TELEGRAM_ALLOWED_USER_IDS is a hard
allowlist checked on every message — keep it to your own Telegram user ID.
Sessions are also confined under LC_PROJECT_ROOT (no `..` escapes) so a
compromised or misbehaving session can't wander outside your projects dir.
`rm`/`sudo` stay off the bash whitelist unless you add them yourself via
LITTLE_CODER_BASH_ALLOW — do that deliberately, not by default.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))  # so `rpc_client` resolves
from rpc_client import PiRpc, PromptResult  # noqa: E402

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("telegram_bridge")

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED_USER_IDS = {int(x) for x in os.environ.get("TELEGRAM_ALLOWED_USER_IDS", "").split(",") if x.strip()}
MODEL = os.environ.get("LC_MODEL", "llamacpp/qwen3.6-35b-a3b")
PROJECT_ROOT = Path(os.environ.get("LC_PROJECT_ROOT", str(Path.home()))).resolve()
IDLE_TIMEOUT_S = int(os.environ.get("LC_IDLE_TIMEOUT_S", "3600"))  # 60 min
PROMPT_TIMEOUT_S = float(os.environ.get("LC_PROMPT_TIMEOUT_S", "1800"))
TELEGRAM_MAX_LEN = 4000  # stay under Telegram's 4096-char hard limit

if not ALLOWED_USER_IDS:
    raise SystemExit("Set TELEGRAM_ALLOWED_USER_IDS — refusing to run with an open allowlist.")


class Session:
    def __init__(self, cwd: Path):
        self.cwd = cwd
        self.rpc = PiRpc(model=MODEL, cwd=str(cwd))
        self.last_used = time.monotonic()
        self.lock = asyncio.Lock()  # serialize turns within one chat

    def close(self):
        self.rpc.close()


sessions: dict[int, Session] = {}


def _resolve_project_dir(subdir: str) -> Path:
    """Resolve subdir under PROJECT_ROOT, rejecting any path escape."""
    candidate = (PROJECT_ROOT / subdir).resolve()
    if PROJECT_ROOT not in candidate.parents and candidate != PROJECT_ROOT:
        raise ValueError("that path escapes the project root")
    if not candidate.is_dir():
        raise ValueError(f"no such directory: {candidate}")
    return candidate


def _chunk(text: str, size: int = TELEGRAM_MAX_LEN) -> list[str]:
    text = text.strip() or "(no output)"
    return [text[i : i + size] for i in range(0, len(text), size)]


def _authorized(update: Update) -> bool:
    user = update.effective_user
    ok = bool(user) and user.id in ALLOWED_USER_IDS
    if not ok and user:
        log.warning("Rejected message from unauthorized user id=%s username=%s", user.id, user.username)
    return ok


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return
    await update.message.reply_text(
        "little-coder bridge up.\n"
        "/new <subdir>  — start a session in <subdir> under the project root\n"
        "/reset         — clear the current session's context\n"
        "/status        — show current session info\n"
        "Anything else is sent to the agent as a coding turn."
    )


async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return
    chat_id = update.effective_chat.id
    subdir = " ".join(context.args) if context.args else "."
    try:
        cwd = _resolve_project_dir(subdir)
    except ValueError as e:
        await update.message.reply_text(f"Can't start there: {e}")
        return

    old = sessions.pop(chat_id, None)
    if old:
        await asyncio.to_thread(old.close)

    sessions[chat_id] = Session(cwd)
    await update.message.reply_text(f"New session in {cwd}")


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return
    chat_id = update.effective_chat.id
    sess = sessions.get(chat_id)
    if not sess:
        await update.message.reply_text("No active session — use /new <subdir> first.")
        return
    async with sess.lock:
        await asyncio.to_thread(sess.rpc.new_session)
        sess.last_used = time.monotonic()
    await update.message.reply_text("Context cleared.")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return
    chat_id = update.effective_chat.id
    sess = sessions.get(chat_id)
    if not sess:
        await update.message.reply_text("No active session — use /new <subdir> first.")
        return
    idle_for = int(time.monotonic() - sess.last_used)
    await update.message.reply_text(f"cwd: {sess.cwd}\nmodel: {MODEL}\nidle: {idle_for}s")


def _format_result(result: PromptResult) -> str:
    parts = []
    if result.assistant_text.strip():
        parts.append(result.assistant_text.strip())
    if result.tool_calls:
        names = ", ".join(tc["name"] for tc in result.tool_calls)
        parts.append(f"\n[{len(result.tool_calls)} tool call(s): {names}]")
    if not parts:
        parts.append("(agent produced no output)")
    return "\n".join(parts)


async def _keep_typing(chat):
    """Telegram's typing indicator auto-expires after ~5s; refresh it for
    as long as a turn is running so long tool-heavy turns don't go visibly
    silent."""
    try:
        while True:
            await chat.send_action("typing")
            await asyncio.sleep(4)
    except asyncio.CancelledError:
        pass


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return
    chat_id = update.effective_chat.id
    sess = sessions.get(chat_id)
    if not sess:
        await update.message.reply_text("No active session — use /new <subdir> first.")
        return

    text = update.message.text
    async with sess.lock:
        sess.last_used = time.monotonic()
        typing_task = asyncio.create_task(_keep_typing(update.message.chat))
        try:
            result = await asyncio.to_thread(sess.rpc.prompt_and_collect, text, PROMPT_TIMEOUT_S)
        except TimeoutError:
            await update.message.reply_text(f"Timed out after {PROMPT_TIMEOUT_S:.0f}s.")
            return
        except RuntimeError as e:
            await update.message.reply_text(f"pi rejected the prompt: {e}")
            return
        finally:
            typing_task.cancel()
        sess.last_used = time.monotonic()

    for chunk in _chunk(_format_result(result)):
        await update.message.reply_text(chunk)


async def reap_idle_sessions(app: Application):
    while True:
        await asyncio.sleep(60)
        now = time.monotonic()
        for chat_id, sess in list(sessions.items()):
            if now - sess.last_used > IDLE_TIMEOUT_S:
                log.info("Reaping idle session for chat %s (cwd=%s)", chat_id, sess.cwd)
                sessions.pop(chat_id, None)
                await asyncio.to_thread(sess.close)
                try:
                    await app.bot.send_message(
                        chat_id,
                        f"Session closed after {IDLE_TIMEOUT_S // 60} min idle "
                        f"(was in {sess.cwd}). Send /new to start again.",
                    )
                except Exception:
                    log.exception("Failed to notify chat %s about reap", chat_id)


async def post_init(app: Application):
    app.create_task(reap_idle_sessions(app))


def main():
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    log.info("Starting bridge — model=%s project_root=%s allowed_users=%s", MODEL, PROJECT_ROOT, ALLOWED_USER_IDS)
    app.run_polling()


if __name__ == "__main__":
    main()
