#!/usr/bin/env python3
"""
scripts/ask_human.py

A tool Claude calls when it needs input from you during an agentic task.

Claude invokes this as a shell command:
    python3 /path/to/scripts/ask_human.py "What directory should I save the output to?"

It connects to the main bot's Unix socket server, which sends your Telegram chat
the question and waits for your reply. The reply is printed to stdout so Claude
receives it as a tool result.

Environment variables:
    ASK_HUMAN_SOCK   Path to the Unix socket (default: <project>/tmp/ask_human.sock)
"""

import os
import socket
import sys
from pathlib import Path

# Default socket path: tmp/ask_human.sock relative to this script's project root
_PROJECT_DIR = Path(__file__).parent.parent
_DEFAULT_SOCK = str(_PROJECT_DIR / "tmp" / "ask_human.sock")


def main() -> int:
    if len(sys.argv) < 2:
        print("ERROR: Usage: ask_human.py <question>", file=sys.stderr)
        return 1

    question = " ".join(sys.argv[1:]).strip()
    if not question:
        print("ERROR: question cannot be empty", file=sys.stderr)
        return 1

    sock_path = os.getenv("ASK_HUMAN_SOCK", _DEFAULT_SOCK)

    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.connect(sock_path)
            s.sendall(question.encode() + b"\n")
            reply = s.makefile().readline().strip()
        print(reply)
        return 0
    except FileNotFoundError:
        print(
            f"ERROR: Bot socket not found at {sock_path}. Is the bot running?",
            file=sys.stderr,
        )
        return 1
    except ConnectionRefusedError:
        print(
            f"ERROR: Bot is not listening on {sock_path}. Was it restarted?",
            file=sys.stderr,
        )
        return 1
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
