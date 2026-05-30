#!/usr/bin/env python3
"""
install_service.py
------------------
Registers crawler.py as a Windows Task Scheduler task that runs
automatically at logon (and once immediately after install).

Run once as a normal user (no admin required for per-user tasks):

    python install_service.py          # install
    python install_service.py remove   # uninstall
"""

import subprocess
import sys
from pathlib import Path

TASK_NAME = "TL_WatchlistDownloader"


def get_python() -> str:
    """Return the absolute path to the current Python interpreter."""
    return sys.executable


def get_script() -> Path:
    return Path(__file__).parent / "crawler.py"


def install():
    python  = get_python()
    script  = get_script()
    workdir = str(script.parent)

    if not script.exists():
        print(f"ERROR: crawler.py not found at {script}")
        sys.exit(1)

    # Build the schtasks command
    cmd = [
        "schtasks", "/Create",
        "/TN", TASK_NAME,
        "/TR", f'"{python}" "{script}"',
        "/SC", "ONLOGON",
        "/RL", "HIGHEST",        # run with highest available privileges
        "/F",                    # force overwrite if exists
        "/SD", workdir,          # start-in directory
    ]

    print(f"Registering scheduled task '{TASK_NAME}' ...")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print("FAILED:")
        print(result.stdout)
        print(result.stderr)
        sys.exit(1)

    print("Task registered successfully.")
    print()

    # Run immediately so it starts without needing a reboot
    run_now = input("Start the crawler now? [Y/n]: ").strip().lower()
    if run_now in ("", "y", "yes"):
        subprocess.Popen(
            [python, str(script)],
            cwd=workdir,
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
        print("Crawler started in a new window.")
    else:
        print("Crawler will start automatically at next logon.")


def remove():
    cmd = ["schtasks", "/Delete", "/TN", TASK_NAME, "/F"]
    print(f"Removing scheduled task '{TASK_NAME}' ...")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print("FAILED (task may not exist):")
        print(result.stderr)
    else:
        print("Task removed.")


def main():
    if len(sys.argv) > 1 and sys.argv[1].lower() == "remove":
        remove()
    else:
        install()


if __name__ == "__main__":
    main()
