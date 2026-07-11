from __future__ import annotations

import os
import stat
from concurrent.futures import ProcessPoolExecutor
from multiprocessing import get_context
from pathlib import Path

import pytest

from polaris.secrets import (
    MAX_SECRETS_FILE_SIZE,
    SecretsFile,
    SecretsFileError,
    parse_secrets,
    runtime_environment,
)


def private_file(path: Path, payload: bytes) -> None:
    path.write_bytes(payload)
    path.chmod(0o600)


def mutate_secrets_file(path: str, prefix: str, remove_existing: bool) -> None:
    secrets = SecretsFile(path)
    for index in range(32):
        secrets.set(f"{prefix}_{index}", f"{prefix.lower()}-value-{index}")
        if remove_existing:
            secrets.remove(f"REMOVE_{index}")


def test_parser_accepts_raw_values_comments_and_spaces() -> None:
    assert parse_secrets(b"# comment\nTOKEN=value with spaces = and #\nEMPTY=\n") == {
        "TOKEN": "value with spaces = and #",
        "EMPTY": "",
    }


@pytest.mark.parametrize(
    "payload",
    [
        b"export TOKEN=value\n",
        b"BAD-NAME=value\n",
        b"TOKEN=$(command)\n",
        b"TOKEN=`command`\n",
        b"TOKEN=first\rsecond\n",
        b"TOKEN=one\nTOKEN=two\n",
        b"TOKEN=\x00value\n",
        b"\xff",
        b"NO_ASSIGNMENT\n",
    ],
)
def test_parser_rejects_invalid_content_without_disclosing_values(payload: bytes) -> None:
    with pytest.raises(SecretsFileError) as raised:
        parse_secrets(payload)
    message = str(raised.value)
    assert "$(command)" not in message
    assert "`command`" not in message
    assert "first\rsecond" not in message
    assert "one" not in message
    assert "two" not in message
    assert "\x00value" not in message


def test_parser_rejects_oversized_file() -> None:
    with pytest.raises(SecretsFileError, match="64 KiB"):
        parse_secrets(b"A=" + b"x" * MAX_SECRETS_FILE_SIZE)


def test_reader_rejects_permissions_and_symlinks(tmp_path: Path) -> None:
    path = tmp_path / "runtime-secrets.env"
    private_file(path, b"TOKEN=sensitive\n")
    path.chmod(0o644)
    with pytest.raises(SecretsFileError, match="0600"):
        SecretsFile(path).read()

    path.chmod(0o600)
    link = tmp_path / "linked.env"
    link.symlink_to(path)
    with pytest.raises(SecretsFileError, match="symlink"):
        SecretsFile(link).read()


def test_set_update_remove_are_atomic_private_and_names_are_redacted(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "runtime-secrets.env"
    secrets = SecretsFile(path)
    secrets.set("TOKEN", "first sensitive value")
    secrets.set("OTHER", "other sensitive value")
    secrets.set("TOKEN", "updated sensitive value")

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert secrets.names() == ("OTHER", "TOKEN")
    assert "sensitive" not in repr(secrets)
    assert "sensitive" not in repr(secrets.read())
    assert not list(path.parent.glob("*.tmp"))
    assert secrets.remove("TOKEN")
    assert not secrets.remove("MISSING")
    assert secrets.read() == {"OTHER": "other sensitive value"}
    assert not list(path.parent.glob("*.tmp"))


def test_two_process_set_and_remove_have_no_lost_updates(tmp_path: Path) -> None:
    path = tmp_path / "runtime-secrets.env"
    private_file(
        path,
        "".join(f"REMOVE_{index}=old\n" for index in range(32)).encode(),
    )
    context = get_context("spawn")

    with ProcessPoolExecutor(max_workers=2, mp_context=context) as pool:
        futures = (
            pool.submit(mutate_secrets_file, str(path), "LEFT", False),
            pool.submit(mutate_secrets_file, str(path), "RIGHT", True),
        )
        for future in futures:
            future.result(timeout=30)

    values = SecretsFile(path).read()
    assert all(values[f"LEFT_{index}"] == f"left-value-{index}" for index in range(32))
    assert all(values[f"RIGHT_{index}"] == f"right-value-{index}" for index in range(32))
    assert not any(name.startswith("REMOVE_") for name in values)
    lock_path = path.parent / f".{path.name}.lock"
    assert stat.S_IMODE(lock_path.stat().st_mode) == 0o600
    assert not list(path.parent.glob("*.tmp"))


def test_lock_timeout_releases_descriptors_for_later_updates(tmp_path: Path) -> None:
    path = tmp_path / "runtime-secrets.env"
    holder = SecretsFile(path)

    with holder._exclusive_lock(), pytest.raises(SecretsFileError, match="timed out"):
        SecretsFile(path, lock_timeout_seconds=0.01).set("BLOCKED", "value")

    SecretsFile(path, lock_timeout_seconds=0.01).set("AFTER", "value")
    assert SecretsFile(path).read() == {"AFTER": "value"}


def test_runtime_environment_process_values_override_file(tmp_path: Path) -> None:
    path = tmp_path / "runtime-secrets.env"
    private_file(path, b"FROM_FILE=file\nOVERRIDE=file\n")

    merged = runtime_environment(path, {"OVERRIDE": "process", "PROCESS_ONLY": "yes"})

    assert merged == {
        "FROM_FILE": "file",
        "OVERRIDE": "process",
        "PROCESS_ONLY": "yes",
    }
    assert "process" not in repr(merged)


def test_reader_rejects_non_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "runtime-secrets.env"
    private_file(path, b"TOKEN=sensitive\n")
    monkeypatch.setattr(os, "geteuid", lambda: path.stat().st_uid + 1)
    with pytest.raises(SecretsFileError, match="owned"):
        SecretsFile(path).read()
