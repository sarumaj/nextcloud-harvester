#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Writes tests/data/rclone-backends.json: the remotes of the rclone the image pins (Dockerfile's
RCLONE_VERSION) with their settings' names and defaults, which test_remote.py checks the app's
lists against. Run it with that rclone after raising the version:

    python3 scripts/rclone_backends.py /path/to/rclone
"""

import json
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]


def main() -> None:
    """Asks the rclone given for its remotes and writes them, with its version."""
    rclone = sys.argv[1] if len(sys.argv) > 1 else "rclone"
    version = re.search(
        r"v(\d+\.\d+\.\d+)", subprocess.run([rclone, "version"], capture_output=True, text=True, check=True).stdout
    )
    providers = json.loads(
        subprocess.run([rclone, "config", "providers"], capture_output=True, text=True, check=True).stdout
    )
    backends = {
        provider["Prefix"]: {
            option["Name"]: str(option.get("Default") or "") for option in provider.get("Options") or []
        }
        for provider in providers
    }
    data = {"version": version.group(1) if version else "", "backends": dict(sorted(backends.items()))}
    target = ROOT / "tests" / "data" / "rclone-backends.json"
    target.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(f"{target}: rclone {data['version']}, {len(backends)} remotes")


if __name__ == "__main__":
    main()
