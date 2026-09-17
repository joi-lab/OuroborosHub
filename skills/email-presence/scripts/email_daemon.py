from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
import signal
import sys

SKILL_ROOT = Path(__file__).resolve().parents[1]
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from lib.client import MailClient
from lib.host_adapter import create_host_adapter
from lib.runtime import EmailRuntime
from lib.store import EmailStore


async def run():
    state = Path(os.environ["OUROBOROS_SKILL_STATE_DIR"])
    path = state / "settings.json"
    cfg = json.loads(path.read_text()) if path.exists() else {}
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            pass  # Host process supervision remains authoritative on Windows.
    host = create_host_adapter(str(cfg.get("binding_id") or ""))
    runtime = EmailRuntime(EmailStore(state), MailClient(), host,
                           folder=cfg.get("folder") or os.environ.get("EMAIL_DEFAULT_FOLDER") or "INBOX",
                           account=os.environ.get("EMAIL_USER", ""),
                           poll_interval=cfg.get("poll_interval_sec") or 30)
    try:
        await runtime.run(stop)
    finally:
        await host.aclose()


if __name__ == "__main__":
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    asyncio.run(run())
