"""Deterministic package verification and safe extraction for the PoC bundle."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import unicodedata
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

FORMAT_VERSION = 1
MAX_ARCHIVE_BYTES = 32 * 1024 * 1024
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_TOTAL_BYTES = 24 * 1024 * 1024
MANIFEST_PATH = "manifest.json"
ALLOWED_FILES = frozenset(
    {
        "backend/services/editorial_generations_poc.py",
        "backend/unit_tests/test_editorial_generations_poc.py",
        "backend/integration_tests/__init__.py",
        "backend/integration_tests/test_editorial_generations_mongodb.py",
        "ops/editorial-generations/Dockerfile.dockerignore",
        "ops/editorial-generations/Dockerfile",
        "ops/editorial-generations/README.md",
        "ops/editorial-generations/cleanup_compose.py",
        "ops/editorial-generations/compose.yaml",
        "ops/editorial-generations/requirements.lock",
        "ops/editorial-generations/run_homologation.py",
        "ops/editorial-generations/run-ci.sh",
        "ops/editorial-generations/test_docker_context.py",
        "ops/editorial-generations/validate_compose.py",
        "ops/editorial-generations/verify_docker_context.py",
        "ops/editorial-generations/run-local.ps1",
    }
)
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
GIT_SHA_RE = re.compile(r"[0-9a-f]{40,64}\Z")


class BundleError(ValueError):
    """The package does not meet the expected integrity or safety contract."""


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def manifest_for(files: dict[str, bytes], source_sha: str) -> dict[str, Any]:
    if not GIT_SHA_RE.fullmatch(source_sha):
        raise BundleError("SHA de origem inválido")
    if set(files) != ALLOWED_FILES:
        missing = sorted(ALLOWED_FILES - set(files))
        unexpected = sorted(set(files) - ALLOWED_FILES)
        raise BundleError(f"Allowlist divergente; ausentes={missing}; inesperados={unexpected}")
    entries = []
    total = 0
    for name in sorted(files):
        _validate_name(name)
        data = files[name]
        if not isinstance(data, bytes) or len(data) > MAX_FILE_BYTES:
            raise BundleError(f"Arquivo inválido ou acima do limite: {name}")
        total += len(data)
        if total > MAX_TOTAL_BYTES:
            raise BundleError("Conteúdo total excede o limite")
        entries.append({"path": name, "size": len(data), "sha256": sha256_bytes(data)})
    content_hash = sha256_bytes(canonical_json(entries))
    package_id = f"editorial-generations-mongodb7-{source_sha[:12]}-{content_hash[:12]}"
    return {
        "format_version": FORMAT_VERSION,
        "package_id": package_id,
        "source": {
            "repository": "Saitar0/trilhacodeprod",
            "git_sha": source_sha,
        },
        "runtime": {"python": "3.11", "pymongo": "4.18.2", "mongodb_major": 7},
        "files": entries,
        "content_sha256": content_hash,
    }


def build_archive(files: dict[str, bytes], source_sha: str) -> bytes:
    manifest = manifest_for(files, source_sha)
    output = __import__("io").BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED, strict_timestamps=True) as archive:
        for name in sorted(files):
            archive.writestr(_zip_info(name), files[name])
        archive.writestr(_zip_info(MANIFEST_PATH), canonical_json(manifest) + b"\n")
    result = output.getvalue()
    if len(result) > MAX_ARCHIVE_BYTES:
        raise BundleError("Pacote gerado excede o limite")
    return result


def verify_archive(
    archive_bytes: bytes, *, expected_archive_sha256: str, expected_source_sha: str
) -> tuple[dict[str, Any], dict[str, bytes]]:
    if not SHA256_RE.fullmatch(expected_archive_sha256):
        raise BundleError("Hash esperado do pacote ausente ou inválido")
    if len(archive_bytes) > MAX_ARCHIVE_BYTES:
        raise BundleError("Pacote excede o limite")
    if sha256_bytes(archive_bytes) != expected_archive_sha256:
        raise BundleError("Hash externo do pacote diverge")
    if not GIT_SHA_RE.fullmatch(expected_source_sha):
        raise BundleError("SHA esperado de origem inválido")
    try:
        with zipfile.ZipFile(__import__("io").BytesIO(archive_bytes), "r") as archive:
            infos = archive.infolist()
            names: list[str] = []
            normalized: set[str] = set()
            total = 0
            for info in infos:
                name = info.filename
                _validate_name(name)
                normalized_name = unicodedata.normalize("NFC", name)
                if normalized_name != name:
                    raise BundleError("Nome de arquivo não está normalizado em NFC")
                folded = normalized_name.casefold()
                if folded in normalized:
                    raise BundleError("Entradas duplicadas ou conflitantes por caixa")
                normalized.add(folded)
                names.append(name)
                if info.is_dir():
                    raise BundleError("Diretórios explícitos não são permitidos")
                mode = info.external_attr >> 16
                if stat.S_ISLNK(mode) or (mode and not stat.S_ISREG(mode)):
                    raise BundleError("Links ou tipos de arquivo não regulares recusados")
                if (
                    info.date_time != (1980, 1, 1, 0, 0, 0)
                    or info.create_system != 3
                    or stat.S_IMODE(mode) != 0o644
                    or info.extra
                    or info.comment
                ):
                    raise BundleError("Metadados ZIP fora do formato determinístico")
                if info.compress_type != zipfile.ZIP_STORED:
                    raise BundleError("Formato de compressão inesperado")
                if info.flag_bits & 0x1:
                    raise BundleError("Entradas ZIP criptografadas não são aceitas")
                if info.file_size > MAX_FILE_BYTES:
                    raise BundleError("Arquivo interno excede o limite")
                total += info.file_size
                if total > MAX_TOTAL_BYTES:
                    raise BundleError("Conteúdo total excede o limite")
            if len(names) != len(set(names)):
                raise BundleError("Entrada duplicada")
            expected_names = ALLOWED_FILES | {MANIFEST_PATH}
            if set(names) != expected_names:
                raise BundleError("Pacote contém arquivos ausentes ou fora da allowlist")
            if names != [*sorted(ALLOWED_FILES), MANIFEST_PATH]:
                raise BundleError("Ordem das entradas não corresponde ao formato determinístico")
            payload = {name: archive.read(name) for name in ALLOWED_FILES}
            manifest_bytes = archive.read(MANIFEST_PATH)
    except (zipfile.BadZipFile, EOFError, OSError, RuntimeError) as exc:
        raise BundleError("Pacote truncado ou ZIP inválido") from exc

    manifest = _strict_json(manifest_bytes)
    if manifest.get("format_version") != FORMAT_VERSION:
        raise BundleError("Versão de formato inesperada")
    source = manifest.get("source")
    if not isinstance(source, dict) or source.get("git_sha") != expected_source_sha:
        raise BundleError("SHA de origem do manifesto diverge do valor fixado")
    if source.get("repository") != "Saitar0/trilhacodeprod":
        raise BundleError("Repositório de origem divergente")
    expected_manifest = manifest_for(payload, expected_source_sha)
    if manifest_bytes != canonical_json(expected_manifest) + b"\n":
        raise BundleError("Manifesto ou conteúdo não corresponde aos hashes declarados")
    return manifest, payload


def extract_verified(
    archive_bytes: bytes,
    destination: Path,
    *,
    expected_archive_sha256: str,
    expected_source_sha: str,
) -> dict[str, Any]:
    manifest, files = verify_archive(
        archive_bytes,
        expected_archive_sha256=expected_archive_sha256,
        expected_source_sha=expected_source_sha,
    )
    if destination.is_symlink():
        raise BundleError("Destino de extração não pode ser symlink")
    destination = destination.resolve()
    if destination.exists():
        raise BundleError("Destino de extração precisa não existir")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".editorial-bundle-", dir=destination.parent
    ) as temporary:
        staging = Path(temporary) / "payload"
        staging.mkdir()
        for name, data in files.items():
            target = staging.joinpath(*PurePosixPath(name).parts)
            resolved = target.resolve()
            if not resolved.is_relative_to(staging.resolve()):
                raise BundleError("Caminho de extração escaparia do destino")
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as output:
                output.write(data)
        os.replace(staging, destination)
    return manifest


def _zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_STORED
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | 0o644) << 16
    info.flag_bits = 0
    return info


def _validate_name(name: str) -> None:
    if not isinstance(name, str) or not name or "\\" in name or "\x00" in name:
        raise BundleError("Nome de caminho inválido")
    if re.match(r"^[A-Za-z]:", name):
        raise BundleError("Caminho absoluto Windows recusado")
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in name.split("/")):
        raise BundleError("Caminho absoluto ou traversal recusado")
    if path.as_posix() != name:
        raise BundleError("Caminho não normalizado")


def _strict_json(data: bytes) -> dict[str, Any]:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise BundleError("Chave duplicada no manifesto")
            result[key] = value
        return result

    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=reject_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BundleError("Manifesto JSON inválido") from exc
    if not isinstance(value, dict):
        raise BundleError("Manifesto não é objeto JSON")
    return value
