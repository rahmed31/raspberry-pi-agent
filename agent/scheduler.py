"""
agent/scheduler.py

Cron scheduling helpers for named agents.
Parses human-readable schedule strings into cron expressions
and manages crontab installation/removal.
"""

import re
import shlex
import subprocess
from typing import Optional


def parse_schedule(schedule_str: str) -> Optional[str]:
    """
    Parse a human-readable schedule string into a cron expression.

    Supported formats:
        daily 8am
        daily 6:30pm
        weekly monday 9am
        weekly fri 5:30pm

    Returns:
        Cron expression string, or None if unparseable.
    """
    s            = schedule_str.strip().lower()
    time_pattern = r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?"

    def parse_time(text: str) -> Optional[tuple]:
        m = re.search(time_pattern, text)
        if not m:
            return None
        hour     = int(m.group(1))
        minute   = int(m.group(2) or 0)
        meridiem = m.group(3) or ""
        if meridiem == "pm" and hour != 12:
            hour += 12
        elif meridiem == "am" and hour == 12:
            hour = 0
        return hour, minute

    if s.startswith("daily"):
        t = parse_time(s)
        if not t:
            return None
        return f"{t[1]} {t[0]} * * *"

    if s.startswith("weekly"):
        days = {
            "monday": 1, "mon": 1,
            "tuesday": 2, "tue": 2,
            "wednesday": 3, "wed": 3,
            "thursday": 4, "thu": 4,
            "friday": 5, "fri": 5,
            "saturday": 6, "sat": 6,
            "sunday": 0, "sun": 0,
        }
        day_num = None
        for day_name, num in days.items():
            if day_name in s:
                day_num = num
                break
        if day_num is None:
            return None
        t = parse_time(s)
        if not t:
            return None
        return f"{t[1]} {t[0]} * * {day_num}"

    return None


def install_cron(agent_name: str, cron_expr: str, agent_main_path: str) -> str:
    """
    Install a cron job for a named agent.

    Returns:
        job_id string used to identify and remove the job later.

    Raises:
        RuntimeError: If crontab installation fails.
    """
    job_id   = f"claude_agent_{agent_name}"
    cron_cmd = (
        f"{cron_expr} /usr/bin/python3 {agent_main_path} "
        f"--scheduled-agent {shlex.quote(agent_name)} "
        f"# {job_id}"
    )

    existing = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    lines    = existing.stdout.splitlines() if existing.returncode == 0 else []
    lines    = [l for l in lines if job_id not in l]
    lines.append(cron_cmd)

    proc = subprocess.run(
        ["crontab", "-"],
        input="\n".join(lines) + "\n",
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"crontab error: {proc.stderr}")

    return job_id


def remove_cron(job_id: str) -> None:
    """Remove a cron job by its job_id comment."""
    try:
        existing = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
        if existing.returncode != 0:
            return
        lines = [l for l in existing.stdout.splitlines() if job_id not in l]
        subprocess.run(
            ["crontab", "-"],
            input="\n".join(lines) + "\n",
            capture_output=True,
            text=True,
        )
    except Exception:
        pass
