"""Read-only local/GPU-host environment check; never print secret values."""
from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys


def command_info(name, args):
    executable = shutil.which(name)
    if not executable:
        return {"available": False, "detail": "not found on PATH"}
    try:
        result = subprocess.run([executable, *args], capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=12,
                                creationflags=0x08000000 if os.name == "nt" else 0)
        lines = (result.stdout or result.stderr).splitlines()
        return {"available": result.returncode == 0, "detail": "\n".join(lines[:5 if name == 'nvidia-smi' else 1])[:2000]}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"available": False, "detail": type(exc).__name__}


def diagnose(path: Path) -> dict:
    packages = {}
    for name in ("fastapi", "uvicorn", "pydantic", "httpx", "paramiko"):
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = None
    existing = path.resolve()
    while not existing.exists() and existing != existing.parent:
        existing = existing.parent
    disk = shutil.disk_usage(existing)
    mode = os.getenv("CUTFLOW_WECOM_MODE", "webhook")
    required = (["CUTFLOW_WECOM_WEBHOOK"] if mode == "webhook" else
                ["CUTFLOW_WECOM_CORP_ID", "CUTFLOW_WECOM_SECRET", "CUTFLOW_WECOM_AGENT_ID", "CUTFLOW_WECOM_TO_USER"])
    report = {
        "python": {"version": platform.python_version(), "supported": sys.version_info >= (3, 10)},
        "system": {"os": platform.system(), "machine": platform.machine()},
        "packages": packages,
        "ffmpeg": command_info("ffmpeg", ["-version"]),
        "ffprobe": command_info("ffprobe", ["-version"]),
        "gpu": command_info("nvidia-smi", ["--query-gpu=name,driver_version,memory.total,memory.used,utilization.gpu", "--format=csv,noheader"]),
        "disk": {"free_gib": round(disk.free / 2**30, 2), "total_gib": round(disk.total / 2**30, 2),
                 "less_than_10_gib_free": disk.free < 10 * 2**30},
        "configuration": {
            "access_token_configured": bool(os.getenv("CUTFLOW_TOKEN")),
            "publish_enabled": os.getenv("CUTFLOW_PUBLISH_ENABLED") == "1",
            "auto_publish_enabled": os.getenv("CUTFLOW_AUTO_PUBLISH") == "1",
            "wecom_mode": mode if mode in {"webhook", "app"} else "invalid",
            "wecom_missing_variables": [key for key in required if not os.getenv(key)],
            "ssh_host_configured": bool(os.getenv("CUTFLOW_SSH_HOST")),
            "ssh_key_configured": bool(os.getenv("CUTFLOW_SSH_KEY")),
            "note": "Only presence is checked. No credentials are printed; no connection or message is sent."
        }
    }
    report["local_edit_ready"] = (report["python"]["supported"] and report["ffmpeg"]["available"]
                                  and report["ffprobe"]["available"] and all(packages.values()))
    report["notes"] = ["GPU discovery does not prove NVENC availability; the renderer separately probes the encoder.",
                       "This report does not establish a working GPU connection or enterprise message delivery."]
    return report


def main():
    parser = argparse.ArgumentParser(description="Read-only CutFlow environment check; no network sends")
    parser.add_argument("--path", type=Path, default=Path(__file__).resolve().parents[1], help="Path used for disk-space check")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    args = parser.parse_args()
    report = diagnose(args.path)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print("CutFlow Studio environment check")
        print("Python:", report["python"]["version"])
        print("Packages:", ", ".join(f"{key}={value or 'MISSING'}" for key,value in report["packages"].items()))
        for key in ("ffmpeg", "ffprobe", "gpu"):
            print(f"{key}: {report[key]['detail']}")
        print(f"Disk free: {report['disk']['free_gib']} GiB")
        print("Configuration (presence only):", json.dumps(report["configuration"], ensure_ascii=False))
        print("Local editing dependencies ready:", report["local_edit_ready"])
    return 0 if report["local_edit_ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
