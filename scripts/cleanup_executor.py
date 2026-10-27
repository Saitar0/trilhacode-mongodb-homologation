"""Remove only this executor run's fixed temporary directories and Compose project."""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
from pathlib import Path

PROJECT_RE = re.compile(r"editorial-poc-([a-f0-9]{32})\Z")
MONGO_URI_RE = re.compile(r"mongodb(?:\+srv)?://[^\s/@:]+:[^\s/@]+@", re.IGNORECASE)
PASSWORD_RE = re.compile(r"(?i)(password|passwd|pwd|token|secret)=([^&\s]+)")
COMPOSE_CLEANUP_TIMEOUT_SECONDS = 240
MAX_DIAGNOSTIC_CHARS = 3000


class CleanupInterrupted(Exception):
    def __init__(self, signum: int):
        self.signum = signum


def sanitize_output(value: object, environment: dict[str, str]) -> str:
    text = value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value or "")
    for key, secret in sorted(environment.items(), key=lambda item: len(item[1]), reverse=True):
        if secret and any(part in key.upper() for part in ("PASSWORD", "TOKEN", "SECRET", "URI")):
            text = text.replace(secret, "[REDACTED]")
    text = MONGO_URI_RE.sub("mongodb://[REDACTED]@", text)
    text = PASSWORD_RE.sub(r"\1=[REDACTED]", text)
    if len(text) > MAX_DIAGNOSTIC_CHARS:
        text = text[:MAX_DIAGNOSTIC_CHARS] + " [TRUNCATED]"
    return text.strip()


def _terminate_group(process: subprocess.Popen[str]) -> None:
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        elif process.poll() is None:
            process.terminate()
        try:
            process.communicate(timeout=3)
            return
        except subprocess.TimeoutExpired:
            pass
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        elif process.poll() is None:
            process.kill()
        try:
            process.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            pass
    except (ProcessLookupError, OSError):
        pass


def _run_compose_cleanup(command: list[str], popen_factory=subprocess.Popen):
    kwargs = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
    }
    if os.name == "posix":
        kwargs["start_new_session"] = True
    elif os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    process = popen_factory(command, **kwargs)
    try:
        stdout, stderr = process.communicate(timeout=COMPOSE_CLEANUP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        _terminate_group(process)
        stdout = exc.output or ""
        stderr = exc.stderr or ""
        return subprocess.CompletedProcess(command, 124, stdout, stderr)
    except BaseException:
        _terminate_group(process)
        raise
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def main(*, popen_factory=subprocess.Popen) -> int:
    runner_temp_value = os.environ.get("RUNNER_TEMP", "")
    runner_temp_source = Path(runner_temp_value) if runner_temp_value else Path()
    if (
        not runner_temp_value
        or not runner_temp_source.is_absolute()
        or runner_temp_source.is_symlink()
    ):
        print("Cleanup recusado: RUNNER_TEMP ausente ou symlink.", file=sys.stderr)
        return 2
    runner_temp = runner_temp_source.resolve()
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
    watched_signals = [signal.SIGINT]
    if hasattr(signal, "SIGTERM"):
        watched_signals.append(signal.SIGTERM)
    previous_handlers = {signum: signal.getsignal(signum) for signum in watched_signals}

    def interrupt(signum, _frame):
        raise CleanupInterrupted(signum)

    try:
        for signum in watched_signals:
            signal.signal(signum, interrupt)
    except ValueError:
        print("Cleanup recusado: sinal só pode ser tratado na thread principal.", file=sys.stderr)
        return 2

    try:
        if match and match.group(1) == run_id and cleanup_needed and bundle.is_dir():
            compose = bundle / "ops" / "editorial-generations" / "compose.yaml"
            cleanup = bundle / "ops" / "editorial-generations" / "cleanup_compose.py"
            protected_bundle_paths = (
                bundle / "ops",
                bundle / "ops" / "editorial-generations",
                compose,
                cleanup,
            )
            if any(path.is_symlink() for path in protected_bundle_paths):
                print("Cleanup recusado: caminho interno é symlink; diretórios mantidos.", file=sys.stderr)
                return 2
            if not compose.is_file() or not cleanup.is_file():
                print("Cleanup recusado: scripts delimitados ausentes; diretórios mantidos.", file=sys.stderr)
                return 2
            try:
                result = _run_compose_cleanup(
                    [sys.executable, str(cleanup), "--compose-file", str(compose)],
                    popen_factory=popen_factory,
                )
            except CleanupInterrupted as exc:
                print(
                    f"Cleanup interrompido por sinal {exc.signum}; "
                    "diretórios e evidências foram mantidos.",
                    file=sys.stderr,
                )
                return 128 + exc.signum
            except KeyboardInterrupt:
                print("Cleanup interrompido; diretórios e evidências foram mantidos.", file=sys.stderr)
                return 130
            except OSError as exc:
                print(
                    f"Cleanup não iniciou ({type(exc).__name__}); diretórios mantidos.",
                    file=sys.stderr,
                )
                return 3
            if result.returncode:
                detail = sanitize_output(result.stderr or result.stdout, dict(os.environ))
                suffix = f" Detalhe: {detail}" if detail else ""
                print(
                    f"Cleanup Docker delimitado falhou (código {result.returncode}); "
                    f"diretórios mantidos.{suffix}",
                    file=sys.stderr,
                )
                return result.returncode
        elif project or run_id:
            if not (match and match.group(1) == run_id and not cleanup_needed):
                print("Cleanup Compose recusado: identidade da execução diverge.", file=sys.stderr)
                return 2

        for target in (bundle, download):
            if target.exists():
                if target.parent != runner_temp:
                    print("Cleanup recusado: destino fora de RUNNER_TEMP.", file=sys.stderr)
                    return 2
                try:
                    shutil.rmtree(target)
                except OSError as exc:
                    print(
                        f"Cleanup de temporários falhou ({type(exc).__name__}); "
                        "evidências restantes foram preservadas.",
                        file=sys.stderr,
                    )
                    return 3
        print("Cleanup: somente os diretórios temporários deste executor foram removidos.")
        return 0
    except CleanupInterrupted as exc:
        print(
            f"Cleanup interrompido por sinal {exc.signum}; diretórios preservados.",
            file=sys.stderr,
        )
        return 128 + exc.signum
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)


if __name__ == "__main__":
    raise SystemExit(main())
