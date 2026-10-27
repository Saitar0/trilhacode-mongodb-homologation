"""Remove only this executor run's fixed temporary directories and Compose project."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_RE = re.compile(r"editorial-poc-([a-f0-9]{32})\Z")


def main() -> int:
    runner_temp = Path(os.environ.get("RUNNER_TEMP", "")).resolve()
    if not runner_temp.is_dir() or runner_temp == Path(runner_temp.anchor):
        print("Cleanup recusado: RUNNER_TEMP inválido.", file=sys.stderr)
        return 2
    bundle = runner_temp / "editorial-generations-bundle"
    download = runner_temp / "editorial-generations-download"
    for target in (bundle, download):
        if target.is_symlink():
            print("Cleanup recusado: caminho temporário é symlink.", file=sys.stderr)
            return 2

    project = os.environ.get("EDITORIAL_POC_PROJECT", "")
    run_id = os.environ.get("EDITORIAL_POC_RUN_ID", "")
    match = PROJECT_RE.fullmatch(project)
    cleanup_needed = os.environ.get("EDITORIAL_POC_CLEANUP_NEEDED") == "true"
    if match and match.group(1) == run_id and cleanup_needed and bundle.is_dir():
        compose = bundle / "ops" / "editorial-generations" / "compose.yaml"
        cleanup = bundle / "ops" / "editorial-generations" / "cleanup_compose.py"
        if compose.is_file() and cleanup.is_file():
            result = subprocess.run(
                [sys.executable, str(cleanup), "--compose-file", str(compose)],
                check=False,
                capture_output=True,
                text=True,
            )
            if result.returncode:
                print("Cleanup Docker delimitado falhou; diretórios mantidos.", file=sys.stderr)
                return result.returncode
    elif project or run_id:
        if match and match.group(1) == run_id and not cleanup_needed:
            pass
        else:
            print("Cleanup Compose recusado: identidade da execução diverge.", file=sys.stderr)
            return 2

    for target in (bundle, download):
        if target.exists():
            if target.parent != runner_temp:
                print("Cleanup recusado: destino fora de RUNNER_TEMP.", file=sys.stderr)
                return 2
            shutil.rmtree(target)
    print("Cleanup: somente os diretórios temporários deste executor foram removidos.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
