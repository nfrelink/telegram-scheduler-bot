#!/usr/bin/env python3
"""Docker healthcheck: verify DB is openable and Telegram API is reachable."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import urllib.request


def check_db() -> None:
    path = os.getenv("DATABASE_PATH", "data/scheduler.db")
    sqlite3.connect(path).close()


def check_telegram() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not token:
        return
    url = f"https://api.telegram.org/bot{token}/getMe"
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=5) as resp:
        data = json.loads(resp.read())
        if not data.get("ok"):
            raise RuntimeError("Telegram getMe returned ok=false")


if __name__ == "__main__":
    try:
        check_db()
        check_telegram()
    except Exception as e:
        print(f"UNHEALTHY: {e}", file=sys.stderr)
        sys.exit(1)
