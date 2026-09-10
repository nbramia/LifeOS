#!/usr/bin/env python3
"""Own one private Chroma child for a candidate test instance.

This wrapper is its own process-group leader.  Its owner-liveness pipe gives
the Chroma child the same portable cancellation/SIGKILL cleanup contract as
the candidate API without reaching a shared Chroma service.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time

import psutil


def _kill_group() -> None:
    try:
        os.killpg(os.getpgrp(), signal.SIGKILL)
    except OSError:
        pass


def _matches(pid: int, started: float, snapshot_root: str) -> bool:
    try:
        process = psutil.Process(pid)
        return (
            abs(process.create_time() - started) <= 1e-6
            and os.path.realpath(process.cwd()) == os.path.realpath(snapshot_root)
        )
    except psutil.Error:
        return False


def _group_live(group_id: int, leader_started: float) -> bool:
    if psutil.pid_exists(group_id):
        try:
            if abs(psutil.Process(group_id).create_time() - leader_started) > 1e-6:
                return False
        except psutil.Error:
            return False
    for process in psutil.process_iter(["pid", "status"]):
        try:
            if process.info["status"] != psutil.STATUS_ZOMBIE and os.getpgid(process.info["pid"]) == group_id:
                return True
        except (OSError, psutil.Error):
            pass
    return False


def _stop_api_from_manifest(instance_dir: str) -> None:
    """Stop only the manifest's exact API session before shared-dir cleanup."""
    try:
        with open(os.path.join(instance_dir, "manifest.json"), encoding="utf-8") as stream:
            manifest = json.load(stream)
        pid = manifest["child_pid"]
        started = manifest["child_start_time"]
        snapshot_root = manifest["snapshot_root"]
        if not isinstance(pid, int) or not isinstance(started, (int, float)):
            return
    except (OSError, ValueError, KeyError, TypeError):
        return
    if not _matches(pid, float(started), snapshot_root):
        return
    try:
        os.killpg(pid, signal.SIGKILL)
    except OSError:
        return
    while _group_live(pid, float(started)):
        time.sleep(0.05)


def _watch_owner(
    fd: int, instance_dir: str, chroma: subprocess.Popen,
    cleanup_started: threading.Event, cleanup_done: threading.Event,
) -> None:
    try:
        os.read(fd, 1)
    except OSError:
        pass
    cleanup_started.set()
    # This wrapper is the one resource owner for the shared instance dir.
    # API's watcher only reaps its own group; clean both services first.
    _stop_api_from_manifest(instance_dir)
    if chroma.poll() is None:
        chroma.kill()
        chroma.wait()
    shutil.rmtree(instance_dir, ignore_errors=True)
    cleanup_done.set()
    _kill_group()
    os._exit(1)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--path", required=True)
    parser.add_argument("--instance-dir", required=True)
    parser.add_argument("--chroma-executable", required=True)
    parser.add_argument("--owner-liveness-fd", required=True, type=int)
    args = parser.parse_args(argv)

    child = subprocess.Popen([
        args.chroma_executable, "run", "--host", args.host, "--port", str(args.port),
        "--path", args.path,
    ])
    cleanup_started = threading.Event()
    cleanup_done = threading.Event()
    threading.Thread(
        target=_watch_owner,
        args=(args.owner_liveness_fd, args.instance_dir, child, cleanup_started, cleanup_done), daemon=True,
        name="lifeos-test-chroma-owner-liveness",
    ).start()

    def stop(_signum, _frame) -> None:
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    returncode = child.wait()
    if cleanup_started.is_set():
        cleanup_done.wait()
    return returncode


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
