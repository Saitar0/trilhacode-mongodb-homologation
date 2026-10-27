"""Fetch one fixed release asset from the executor repo, verify, then extract."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from bundle_security import (
    MAX_ARCHIVE_BYTES,
    BundleError,
    extract_verified,
    verify_archive,
)

SOURCE_SHA = re.compile(r"[0-9a-f]{40,64}\Z")
HASH = re.compile(r"[0-9a-f]{64}\Z")


def load_lock(path: Path) -> dict:
    def reject_duplicate_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise BundleError("Chave duplicada no lock")
            result[key] = value
        return result

    try:
        lock = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=reject_duplicate_keys,
        )
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise BundleError("Lock de pacote inválido") from exc
    if not isinstance(lock, dict) or set(lock) != {"format_version", "transport", "package"}:
        raise BundleError("Estrutura do lock inesperada")
    if type(lock["format_version"]) is not int or lock["format_version"] != 1:
        raise BundleError("Versão de lock não suportada")
    package = lock["package"]
    transport = lock["transport"]
    if set(package) != {"source_repository", "source_sha", "sha256", "python", "pymongo", "mongodb_major", "format_version"}:
        raise BundleError("Campos de pacote inesperados")
    if set(transport) != {"enabled", "executor_repository", "release_tag", "asset_name"}:
        raise BundleError("Campos de transporte inesperados")
    if type(package.get("format_version")) is not int or package["format_version"] != 1:
        raise BundleError("Versão de formato do pacote não suportada")
    if type(transport["enabled"]) is not bool or not all(
        isinstance(transport[field], str)
        for field in ("executor_repository", "release_tag", "asset_name")
    ):
        raise BundleError("Tipos do transporte inválidos")
    if package["source_repository"] != "Saitar0/trilhacodeprod":
        raise BundleError("Repositório de origem divergente")
    source_sha = package["source_sha"]
    if not isinstance(source_sha, str) or not SOURCE_SHA.fullmatch(source_sha):
        raise BundleError("SHA de origem não configurado")
    if not isinstance(package["sha256"], str) or not HASH.fullmatch(package["sha256"]):
        raise BundleError("Hash externo do pacote não configurado")
    expected_tag = f"editorial-generations-mongodb7-{source_sha[:12]}"
    expected_asset = f"editorial-generations-mongodb7-{source_sha}.zip"
    if transport["release_tag"] != expected_tag or transport["asset_name"] != expected_asset:
        raise BundleError("Tag ou nome de asset diverge da convenção fixa")
    if package != {
        "source_repository": "Saitar0/trilhacodeprod",
        "source_sha": source_sha,
        "sha256": package["sha256"],
        "python": "3.11",
        "pymongo": "4.18.2",
        "mongodb_major": 7,
        "format_version": 1,
    }:
        raise BundleError("Versões de runtime divergem do contrato")
    return lock


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lock", required=True, type=Path)
    parser.add_argument("--destination", required=True, type=Path)
    args = parser.parse_args()
    try:
        lock = load_lock(args.lock)
        transport = lock["transport"]
        if transport["enabled"] is not True:
            raise BundleError("Transporte está desabilitado até autorização e revisão do hash")
        repository = os.environ.get("GITHUB_REPOSITORY", "")
        if not repository or repository != transport["executor_repository"]:
            raise BundleError("Execução fora do repositório executor fixado")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise BundleError("Identidade de repositório executor inválida")
        if not os.environ.get("GH_TOKEN"):
            raise BundleError("Token de leitura efêmero do workflow ausente")

        download_dir = args.destination.parent / "editorial-generations-download"
        download_dir.mkdir(parents=True, exist_ok=False)
        release = subprocess.run(
            [
                "gh", "release", "view", transport["release_tag"],
                "--repo", repository, "--json", "assets",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if release.returncode:
            raise BundleError("Metadados da release fixada não puderam ser lidos")
        try:
            release_data = json.loads(release.stdout)
        except json.JSONDecodeError as exc:
            raise BundleError("Metadados da release têm JSON inválido") from exc
        if not isinstance(release_data, dict):
            raise BundleError("Metadados da release têm formato inesperado")
        assets = [
            asset for asset in release_data.get("assets", [])
            if asset.get("name") == transport["asset_name"]
        ]
        if len(assets) != 1 or type(assets[0].get("size")) is not int:
            raise BundleError("Release não contém um único asset com o nome fixado")
        if assets[0]["size"] > MAX_ARCHIVE_BYTES:
            raise BundleError("Asset da release excede o limite do pacote")
        result = subprocess.run(
            [
                "gh", "release", "download", transport["release_tag"],
                "--repo", repository,
                "--pattern", transport["asset_name"],
                "--dir", str(download_dir),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode:
            raise BundleError("Download do asset fixado falhou; saída omitida")
        archive_path = download_dir / transport["asset_name"]
        if not archive_path.is_file() or archive_path.is_symlink():
            raise BundleError("Asset esperado não foi recebido como arquivo regular")
        if archive_path.stat().st_size > MAX_ARCHIVE_BYTES:
            raise BundleError("Asset baixado excede o limite do pacote")
        archive_bytes = archive_path.read_bytes()
        manifest, _verified_files = verify_archive(
            archive_bytes,
            expected_archive_sha256=lock["package"]["sha256"],
            expected_source_sha=lock["package"]["source_sha"],
        )
        if manifest.get("format_version") != lock["package"]["format_version"]:
            raise BundleError("Versão do formato do pacote diverge do lock")
        manifest = extract_verified(
            archive_bytes,
            args.destination,
            expected_archive_sha256=lock["package"]["sha256"],
            expected_source_sha=lock["package"]["source_sha"],
        )
        output = os.environ.get("GITHUB_OUTPUT")
        if output:
            with open(output, "a", encoding="utf-8") as handle:
                handle.write(f"package_id={manifest['package_id']}\n")
                handle.write(f"source_sha={manifest['source']['git_sha']}\n")
        print("Pacote verificado por hash externo e extraído após validação integral.")
        print(f"Package ID: {manifest['package_id']}")
        print(f"Source SHA: {manifest['source']['git_sha']}")
        return 0
    except (BundleError, OSError, subprocess.SubprocessError, TypeError) as exc:
        print(f"Preparação do pacote bloqueada: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
