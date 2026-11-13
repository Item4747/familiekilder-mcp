"""Crash-safe installation of verified provider profiles.

A bundle is read from a private file here; a program that fetches bundles
from elsewhere passes the bytes to `onboard_from_bytes`. The result lists the
login commands to run next, prefixed with `command`: how the user starts the
program (default `familiekilder-mcp`).
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import stat
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Mapping

from .core import PROVIDER_SOURCES, project_paths
from .private_lock import private_source_lock
from .private_state import (
    PrivateStateError,
    atomic_write_private_bytes,
    ensure_private_directory,
    fsync_directory,
    read_private_bytes,
    read_private_json,
    remove_private_file,
    validate_private_parent_tree,
)
from .provider_contract import (
    _IMMUTABLE_FIELDS,
    _KEY_ID,
    _SHA256,
    MAX_BUNDLE_BYTES,
    ProviderBundleError,
    VerifiedProviderBundle,
    _canonical_json,
    _decode_json,
    _parse_timestamp,
    _validate_provenance,
    _validate_source_config,
    format_timestamp,
    verify_provider_bundle,
)

STATE_SCHEMA = "family.provider-state/v1"
TRANSACTION_SCHEMA = "family.provider-transaction/v1"
_TRANSACTION_ID = re.compile(r"[0-9a-f]{64}\Z")
_PROFILE_FILES = {
    "vigilo": "vigilo-client.json",
    "skolemelding": "skolemelding-client.json",
    "spond": "spond-provider.json",
    "hoopit": "hoopit-client.json",
    "ist-home": "ist-home-client.json",
}
_SESSION_FILES = {
    "vigilo": "vigilo-session.json",
    "skolemelding": "skolemelding-session.json",
    "spond": "spond-session.json",
    "hoopit": "hoopit-session.json",
    "ist-home": "ist-home-session.json",
}
_TRANSACTION_TARGET_IDS = (*_PROFILE_FILES, "provider-state")

# How the user starts the program, in the login commands of `next_steps`.
DEFAULT_COMMAND = "familiekilder-mcp"


def provider_state_path(root: Path | str | None) -> Path:
    return project_paths(root).private / "provider" / "provider-state.json"


def provider_transaction_path(root: Path | str | None) -> Path:
    """Return the fixed private directory used for one recoverable import."""

    return project_paths(root).private / "provider" / "provisioning-transaction"


def _profile_path(root: Path | str | None, source: str) -> Path:
    return project_paths(root).private / "credentials" / _PROFILE_FILES[source]


def _session_path(root: Path | str | None, source: str) -> Path:
    return project_paths(root).private / "credentials" / _SESSION_FILES[source]


def _path_present(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        raise ProviderBundleError("Privat providertilstand kunne ikke kontrolleres") from None
    return True


def _read_optional_private(path: Path, *, maximum: int = MAX_BUNDLE_BYTES) -> bytes | None:
    if not _path_present(path):
        return None
    try:
        return read_private_bytes(path, max_bytes=maximum)
    except PrivateStateError as exc:
        raise ProviderBundleError(str(exc)) from None


def _transaction_directory_exists(root: Path | str | None) -> bool:
    """Check the transaction sentinel without following any private symlink."""

    transaction = provider_transaction_path(root)
    candidates = (
        project_paths(root).private,
        transaction.parent,
        transaction,
    )
    for candidate in candidates:
        try:
            metadata = candidate.lstat()
        except FileNotFoundError:
            # Do not reject or repair a broad legacy .private mode merely to
            # establish that no provider transaction exists. Normal command
            # setup retains responsibility for securing that layout.
            return False
        except OSError:
            raise ProviderBundleError(
                "Providerprofiltransaksjonen kunne ikke kontrolleres sikkert"
            ) from None
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or (hasattr(os, "geteuid") and metadata.st_uid != os.geteuid())
        ):
            raise ProviderBundleError(
                "Providerprofiltransaksjonen har en utrygg katalogstruktur"
            )
    try:
        # A synthetic child makes the existing transaction directory part of
        # the validated parent tree. At this point every component exists and
        # has already been checked without following a symlink.
        return validate_private_parent_tree(transaction / ".presence-check")
    except PrivateStateError as exc:
        raise ProviderBundleError(str(exc)) from None


def _transaction_targets(root: Path | str | None) -> dict[str, Path]:
    targets = {source: _profile_path(root, source) for source in PROVIDER_SOURCES}
    targets["provider-state"] = provider_state_path(root)
    return targets


def _transaction_stage_path(transaction: Path, target_id: str, kind: str) -> Path:
    if target_id not in _TRANSACTION_TARGET_IDS or kind not in {"old", "new"}:
        raise ProviderBundleError("Providerprofiltransaksjonen har ugyldig filreferanse")
    return transaction / f"{kind}-{target_id}.bin"


def _transaction_manifest_path(transaction: Path) -> Path:
    return transaction / "manifest.json"


def _transaction_temporary_name(name: str) -> bool:
    durable_names = {
        "manifest.json",
        *(
            f"{kind}-{target_id}.bin"
            for target_id in _TRANSACTION_TARGET_IDS
            for kind in ("old", "new")
        ),
    }
    return any(
        name.startswith(f".{durable_name}.") and name.endswith(".tmp")
        for durable_name in durable_names
    )


def _validated_transaction_files(
    transaction: Path,
    *,
    expected: set[str] | None,
) -> list[Path]:
    """List only owned 0600 regular files in the fixed staging directory."""

    try:
        children = list(transaction.iterdir())
    except OSError:
        raise ProviderBundleError(
            "Providerprofiltransaksjonen kunne ikke kontrolleres sikkert"
        ) from None
    names = {child.name for child in children}
    if len(names) != len(children):
        raise ProviderBundleError(
            "Providerprofiltransaksjonen har tvetydig filsett"
        )
    if expected is not None and names != expected:
        raise ProviderBundleError(
            "Providerprofiltransaksjonen har ufullstendig staging"
        )
    if expected is None:
        durable_names = {
            "manifest.json",
            *(
                f"{kind}-{target_id}.bin"
                for target_id in _TRANSACTION_TARGET_IDS
                for kind in ("old", "new")
            ),
        }
        if any(
            child.name not in durable_names
            and not _transaction_temporary_name(child.name)
            for child in children
        ):
            raise ProviderBundleError(
                "Providerprofiltransaksjonen har ukjente staging-filer"
            )
    for child in children:
        try:
            metadata = child.lstat()
        except OSError:
            raise ProviderBundleError(
                "Providerprofiltransaksjonen kunne ikke kontrolleres sikkert"
            ) from None
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or (os.name != "nt" and metadata.st_mode & 0o077)
            or (hasattr(os, "geteuid") and metadata.st_uid != os.geteuid())
        ):
            raise ProviderBundleError(
                "Providerprofiltransaksjonen har utrygg staging-fil"
            )
    return children


def _remove_transaction_directory(
    transaction: Path,
    *,
    expected: set[str] | None,
) -> None:
    """Remove a transaction durably, invalidating its manifest first."""

    children = _validated_transaction_files(transaction, expected=expected)
    manifest = _transaction_manifest_path(transaction)
    if manifest in children:
        try:
            remove_private_file(manifest)
        except PrivateStateError as exc:
            raise ProviderBundleError(str(exc)) from None
        children.remove(manifest)
    for child in children:
        try:
            remove_private_file(child)
        except PrivateStateError as exc:
            raise ProviderBundleError(str(exc)) from None
    try:
        transaction.rmdir()
        fsync_directory(transaction.parent)
    except (OSError, PrivateStateError):
        raise ProviderBundleError(
            "Providerprofiltransaksjonen kunne ikke ryddes sikkert"
        ) from None


def _validate_transaction_manifest(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {
        "schema",
        "transaction_id",
        "entries",
    }:
        raise ProviderBundleError(
            "Providerprofiltransaksjonen har ugyldig manifest"
        )
    if value.get("schema") != TRANSACTION_SCHEMA:
        raise ProviderBundleError(
            "Providerprofiltransaksjonen har ukjent kontrakt"
        )
    transaction_id = value.get("transaction_id")
    if not isinstance(transaction_id, str) or not _TRANSACTION_ID.fullmatch(
        transaction_id
    ):
        raise ProviderBundleError(
            "Providerprofiltransaksjonen har ugyldig transaksjons-ID"
        )
    raw_entries = value.get("entries")
    if not isinstance(raw_entries, Mapping) or set(raw_entries) != set(
        _TRANSACTION_TARGET_IDS
    ):
        raise ProviderBundleError(
            "Providerprofiltransaksjonen mangler mål"
        )
    entries: dict[str, dict[str, Any]] = {}
    for target_id in _TRANSACTION_TARGET_IDS:
        raw_entry = raw_entries.get(target_id)
        if not isinstance(raw_entry, Mapping) or set(raw_entry) != {
            "old_present",
            "old_sha256",
            "new_sha256",
        }:
            raise ProviderBundleError(
                "Providerprofiltransaksjonen har ugyldig målmetadata"
            )
        old_present = raw_entry.get("old_present")
        old_sha256 = raw_entry.get("old_sha256")
        new_sha256 = raw_entry.get("new_sha256")
        if not isinstance(old_present, bool):
            raise ProviderBundleError(
                "Providerprofiltransaksjonen har ugyldig målmetadata"
            )
        if old_present:
            if not isinstance(old_sha256, str) or not _SHA256.fullmatch(old_sha256):
                raise ProviderBundleError(
                    "Providerprofiltransaksjonen har ugyldig backupdigest"
                )
        elif old_sha256 is not None:
            raise ProviderBundleError(
                "Providerprofiltransaksjonen har ugyldig fraværsmarkør"
            )
        if not isinstance(new_sha256, str) or not _SHA256.fullmatch(new_sha256):
            raise ProviderBundleError(
                "Providerprofiltransaksjonen har ugyldig stagingdigest"
            )
        entries[target_id] = {
            "old_present": old_present,
            "old_sha256": old_sha256,
            "new_sha256": new_sha256,
        }
    return {
        "schema": TRANSACTION_SCHEMA,
        "transaction_id": transaction_id,
        "entries": entries,
    }


def _load_state(root: Path | str | None) -> Mapping[str, Any] | None:
    path = provider_state_path(root)
    if not _path_present(path):
        return None
    try:
        value = read_private_json(path, max_bytes=MAX_BUNDLE_BYTES)
    except PrivateStateError as exc:
        raise ProviderBundleError(str(exc)) from None
    if not isinstance(value, Mapping) or set(value) != {
        "schema",
        "generation",
        "bundle_digest",
        "payload_sha256",
        "key_id",
        "expires_at",
        "provisioned_at",
        "sources",
    }:
        raise ProviderBundleError("Den private providertilstanden har ugyldig format")
    generation = value.get("generation")
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
        raise ProviderBundleError("Den private providertilstanden har ugyldig generasjon")
    if value.get("schema") != STATE_SCHEMA:
        raise ProviderBundleError("Den private providertilstanden har ukjent kontrakt")
    for field_name in ("bundle_digest", "payload_sha256"):
        if not isinstance(value.get(field_name), str) or not _SHA256.fullmatch(value[field_name]):
            raise ProviderBundleError("Den private providertilstanden har ugyldig digest")
    if not isinstance(value.get("key_id"), str) or not _KEY_ID.fullmatch(value["key_id"]):
        raise ProviderBundleError("Den private providertilstanden har ugyldig key_id")
    _parse_timestamp(value.get("expires_at"), "lokal expires_at")
    _parse_timestamp(value.get("provisioned_at"), "lokal provisioned_at")
    source_state = value.get("sources")
    if not isinstance(source_state, Mapping) or set(source_state) != set(PROVIDER_SOURCES):
        raise ProviderBundleError("Den private providertilstanden mangler kilder")
    for source, metadata in source_state.items():
        if not isinstance(metadata, Mapping) or set(metadata) != {
            "config_sha256",
            "immutable_sha256",
            "provenance",
        }:
            raise ProviderBundleError("Den private providertilstanden har ugyldig kildeformat")
        if (
            not isinstance(metadata.get("config_sha256"), str)
            or not _SHA256.fullmatch(metadata["config_sha256"])
            or not isinstance(metadata.get("immutable_sha256"), str)
            or not _SHA256.fullmatch(metadata["immutable_sha256"])
        ):
            raise ProviderBundleError("Den private providertilstanden har ugyldig kildemetadata")
        _validate_provenance(source, metadata.get("provenance"))
    return value


def _config_bytes(config: Mapping[str, Any]) -> bytes:
    return _canonical_json(config) + b"\n"


def _immutable_digest(source: str, config: Mapping[str, Any]) -> str:
    identity = {field_name: config.get(field_name) for field_name in _IMMUTABLE_FIELDS[source]}
    return hashlib.sha256(_canonical_json(identity)).hexdigest()


def _state_value(
    bundle: VerifiedProviderBundle,
    *,
    provisioned_at: datetime,
) -> dict[str, Any]:
    source_state: dict[str, Any] = {}
    for source in PROVIDER_SOURCES:
        config = bundle.sources[source]["config"]
        provenance = bundle.sources[source]["provenance"]
        source_state[source] = {
            "config_sha256": hashlib.sha256(_config_bytes(config)).hexdigest(),
            "immutable_sha256": _immutable_digest(source, config),
            "provenance": provenance,
        }
    return {
        "schema": STATE_SCHEMA,
        "generation": bundle.generation,
        "bundle_digest": bundle.bundle_digest,
        "payload_sha256": bundle.payload_sha256,
        "key_id": bundle.key_id,
        "expires_at": format_timestamp(bundle.expires_at),
        "provisioned_at": format_timestamp(provisioned_at),
        "sources": source_state,
    }


def _validate_generation(bundle: VerifiedProviderBundle, state: Mapping[str, Any] | None) -> None:
    if state is None:
        return
    current_generation = state["generation"]
    if bundle.generation < current_generation:
        raise ProviderBundleError("Providerpakken er en rollback til en eldre generasjon")
    if bundle.generation == current_generation and bundle.bundle_digest != state["bundle_digest"]:
        raise ProviderBundleError("Providerpakken kolliderer med samme lokale generasjon")


def _validate_immutable_updates(
    root: Path | str | None,
    bundle: VerifiedProviderBundle,
    state: Mapping[str, Any] | None,
) -> None:
    for source in PROVIDER_SOURCES:
        incoming = bundle.sources[source]["config"]
        incoming_digest = _immutable_digest(source, incoming)
        current_path = _profile_path(root, source)
        current_raw = _read_optional_private(current_path)
        current_digest: str | None = None
        if current_raw is not None:
            current_value = _decode_json(current_raw, label=f"Lokal {source}-providerprofil")
            current_config = _validate_source_config(source, current_value)
            current_digest = _immutable_digest(source, current_config)
        elif state is not None:
            current_digest = state["sources"][source]["immutable_sha256"]
        if current_digest is None or current_digest == incoming_digest:
            continue
        if _path_present(_session_path(root, source)):
            raise ProviderBundleError(
                f"{source}: immutable auth-identitet er endret; logg ut før providerpakken installeres"
            )


def _prepare_profile_transaction(
    root: Path | str | None,
    files: Mapping[Path, bytes],
) -> set[str]:
    """Durably stage before-images and new files, then publish one manifest."""

    targets = _transaction_targets(root)
    if set(files) != set(targets.values()):
        raise ProviderBundleError(
            "Providerprofiltransaksjonen har et ugyldig målsett"
        )
    transaction = provider_transaction_path(root)
    if _transaction_directory_exists(root):
        raise ProviderBundleError(
            "En uferdig providerprofiltransaksjon må gjenopprettes først"
        )
    try:
        ensure_private_directory(transaction)
    except PrivateStateError as exc:
        raise ProviderBundleError(str(exc)) from None

    entries: dict[str, dict[str, Any]] = {}
    expected = {"manifest.json"}
    try:
        for target_id in _TRANSACTION_TARGET_IDS:
            target = targets[target_id]
            old_payload = _read_optional_private(target)
            new_payload = files[target]
            new_stage = _transaction_stage_path(transaction, target_id, "new")
            atomic_write_private_bytes(new_stage, new_payload, overwrite=False)
            expected.add(new_stage.name)
            old_present = old_payload is not None
            old_digest: str | None = None
            if old_payload is not None:
                old_stage = _transaction_stage_path(transaction, target_id, "old")
                atomic_write_private_bytes(old_stage, old_payload, overwrite=False)
                expected.add(old_stage.name)
                old_digest = hashlib.sha256(old_payload).hexdigest()
            entries[target_id] = {
                "old_present": old_present,
                "old_sha256": old_digest,
                "new_sha256": hashlib.sha256(new_payload).hexdigest(),
            }
        manifest = {
            "schema": TRANSACTION_SCHEMA,
            "transaction_id": secrets.token_hex(32),
            "entries": entries,
        }
        atomic_write_private_bytes(
            _transaction_manifest_path(transaction),
            _config_bytes(manifest),
            overwrite=False,
        )
    except BaseException as exc:
        # No destination is ever touched before the manifest exists. An
        # incomplete, manifest-less staging area is therefore always safe to
        # remove, including after an interrupted staging-file write.
        if not _path_present(_transaction_manifest_path(transaction)):
            try:
                _remove_transaction_directory(transaction, expected=None)
            except Exception:
                raise ProviderBundleError(
                    "Providerprofiltransaksjonen kunne ikke stages eller ryddes sikkert"
                ) from None
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        if isinstance(exc, ProviderBundleError):
            raise
        raise ProviderBundleError(
            "Providerprofiltransaksjonen kunne ikke stages sikkert"
        ) from None
    return expected


def _load_profile_transaction(
    root: Path | str | None,
) -> tuple[dict[str, dict[str, Any]], dict[str, bytes], set[str]]:
    transaction = provider_transaction_path(root)
    manifest_path = _transaction_manifest_path(transaction)
    try:
        raw_manifest_bytes = read_private_bytes(
            manifest_path,
            max_bytes=64 * 1024,
            missing_message="Providerprofiltransaksjonen mangler manifest",
        )
    except PrivateStateError as exc:
        raise ProviderBundleError(str(exc)) from None
    raw_manifest = _decode_json(
        raw_manifest_bytes,
        label="Providerprofiltransaksjonsmanifestet",
    )
    manifest = _validate_transaction_manifest(raw_manifest)
    entries = manifest["entries"]
    expected = {"manifest.json"}
    old_payloads: dict[str, bytes] = {}
    for target_id in _TRANSACTION_TARGET_IDS:
        entry = entries[target_id]
        new_stage = _transaction_stage_path(transaction, target_id, "new")
        expected.add(new_stage.name)
        try:
            new_payload = read_private_bytes(new_stage, max_bytes=MAX_BUNDLE_BYTES)
        except PrivateStateError as exc:
            raise ProviderBundleError(str(exc)) from None
        if not hashlib.sha256(new_payload).hexdigest() == entry["new_sha256"]:
            raise ProviderBundleError(
                "Providerprofiltransaksjonen har endret staginginnhold"
            )
        if entry["old_present"]:
            old_stage = _transaction_stage_path(transaction, target_id, "old")
            expected.add(old_stage.name)
            try:
                old_payload = read_private_bytes(
                    old_stage,
                    max_bytes=MAX_BUNDLE_BYTES,
                )
            except PrivateStateError as exc:
                raise ProviderBundleError(str(exc)) from None
            if not hashlib.sha256(old_payload).hexdigest() == entry["old_sha256"]:
                raise ProviderBundleError(
                    "Providerprofiltransaksjonen har endret backupinnhold"
                )
            old_payloads[target_id] = old_payload
    _validated_transaction_files(transaction, expected=expected)
    return entries, old_payloads, expected


def _remove_target_temporaries(target: Path) -> None:
    """Remove only atomic-writer leftovers for one controlled transaction target."""

    prefix = f".{target.name}."
    try:
        candidates = [
            child
            for child in target.parent.iterdir()
            if child.name.startswith(prefix) and child.name.endswith(".tmp")
        ]
    except OSError:
        raise ProviderBundleError(
            "Midlertidige providerprofilfiler kunne ikke kontrolleres sikkert"
        ) from None
    for candidate in candidates:
        try:
            metadata = candidate.lstat()
        except OSError:
            raise ProviderBundleError(
                "Midlertidige providerprofilfiler kunne ikke kontrolleres sikkert"
            ) from None
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or (os.name != "nt" and metadata.st_mode & 0o077)
            or (hasattr(os, "geteuid") and metadata.st_uid != os.geteuid())
        ):
            raise ProviderBundleError(
                "En midlertidig providerprofilfil har utrygg type eller modus"
            )
        try:
            remove_private_file(candidate)
        except PrivateStateError as exc:
            raise ProviderBundleError(str(exc)) from None


def _recover_profile_transaction_locked(root: Path | str | None) -> bool:
    """Rollback one prepared transaction while every source lock is held."""

    if not _transaction_directory_exists(root):
        return False
    transaction = provider_transaction_path(root)
    manifest_path = _transaction_manifest_path(transaction)
    if not _path_present(manifest_path):
        # Staging was interrupted before the commit intent became durable, or
        # cleanup was interrupted after it was invalidated. In both cases no
        # rollback remains necessary.
        _remove_transaction_directory(transaction, expected=None)
        return False

    entries, old_payloads, expected = _load_profile_transaction(root)
    targets = _transaction_targets(root)
    try:
        # Profiles are restored before the monotonic state file. The manifest
        # remains the authoritative "do not use" sentinel until every restore
        # and directory fsync has succeeded.
        for target_id in _TRANSACTION_TARGET_IDS:
            target = targets[target_id]
            if entries[target_id]["old_present"]:
                atomic_write_private_bytes(
                    target,
                    old_payloads[target_id],
                    overwrite=True,
                )
            else:
                remove_private_file(target)
            _remove_target_temporaries(target)
        _remove_transaction_directory(transaction, expected=expected)
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        # Keep the intact manifest and backups so the exact same rollback can
        # be retried after another restart.
        raise ProviderBundleError(
            "Providerprofiltransaksjonen kunne ikke gjenopprettes sikkert"
        ) from None
    return True


def recover_pending_provider_transaction(root: Path | str | None) -> bool:
    """Recover an interrupted provider import before other CLI work proceeds."""

    if not _transaction_directory_exists(root):
        return False
    paths = project_paths(root)
    with ExitStack() as stack:
        for source in PROVIDER_SOURCES:
            stack.enter_context(private_source_lock(paths.root, source))
        return _recover_profile_transaction_locked(paths.root)


def _commit_profile_transaction(
    root: Path | str | None,
    files: Mapping[Path, bytes],
    *,
    writer: Callable[..., None] = atomic_write_private_bytes,
) -> None:
    expected = _prepare_profile_transaction(root, files)
    transaction = provider_transaction_path(root)
    try:
        for path, payload in files.items():
            writer(path, payload, overwrite=True)
    except BaseException as exc:
        try:
            _recover_profile_transaction_locked(root)
        except BaseException:
            raise ProviderBundleError(
                "Providerprofiltransaksjonen feilet og kunne ikke rulles tilbake sikkert"
            ) from None
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        if isinstance(exc, ProviderBundleError):
            raise
        raise ProviderBundleError("Providerprofilene kunne ikke provisioneres atomisk") from None
    try:
        # Removing the manifest is the commit point. All destination writes
        # and parent-directory fsyncs have completed before it is invalidated.
        _remove_transaction_directory(transaction, expected=expected)
    except Exception:
        raise ProviderBundleError(
            "Providerprofiltransaksjonen kunne ikke ferdigstilles sikkert"
        ) from None


def provision_provider_bundle(
    root: Path | str | None,
    bundle: VerifiedProviderBundle,
    *,
    now: datetime | None = None,
    writer: Callable[..., None] = atomic_write_private_bytes,
    command: str = DEFAULT_COMMAND,
) -> dict[str, Any]:
    """Transactionally provision every profile and monotonic local state."""

    checked_now = now or datetime.now(UTC)
    if checked_now.tzinfo is None:
        raise ProviderBundleError("Provisioneringstidspunktet mangler tidssone")
    if bundle.expires_at <= checked_now.astimezone(UTC):
        raise ProviderBundleError("Providerpakken er utløpt")
    paths = project_paths(root)
    ensure_private_directory(paths.private / "credentials")
    ensure_private_directory(paths.private / "provider")
    with ExitStack() as stack:
        for source in PROVIDER_SOURCES:
            stack.enter_context(private_source_lock(paths.root, source))
        _recover_profile_transaction_locked(paths.root)
        state = _load_state(paths.root)
        _validate_generation(bundle, state)
        _validate_immutable_updates(paths.root, bundle, state)
        profile_files: dict[Path, bytes] = {
            _profile_path(paths.root, source): _config_bytes(bundle.sources[source]["config"])
            for source in PROVIDER_SOURCES
        }
        if (
            state is not None
            and state["generation"] == bundle.generation
            and state["bundle_digest"] == bundle.bundle_digest
            and all(_read_optional_private(path) == payload for path, payload in profile_files.items())
        ):
            changed = False
        else:
            if state is not None and state["generation"] == bundle.generation:
                provisioned_at = _parse_timestamp(state["provisioned_at"], "lokal provisioned_at")
            else:
                provisioned_at = checked_now.astimezone(UTC)
            state_bytes = _config_bytes(_state_value(bundle, provisioned_at=provisioned_at))
            files = {**profile_files, provider_state_path(paths.root): state_bytes}
            _commit_profile_transaction(paths.root, files, writer=writer)
            changed = True
    return {
        "onboarded": True,
        "changed": changed,
        "generation": bundle.generation,
        "expires_at": format_timestamp(bundle.expires_at),
        "sources": list(PROVIDER_SOURCES),
        "next_steps": [
            {
                "source": "vigilo",
                "action": "browser-callback-login",
                "command": f"{command} auth vigilo browser-login",
                "detail": (
                    "Fullfør ID-porten/BankID i et isolert nettleservindu; "
                    "appklientens callback og tokens vises ikke"
                ),
            },
            {
                "source": "skolemelding",
                "action": "manual-login",
                "command": f"{command} auth skolemelding login",
            },
            {
                "source": "spond",
                "action": "isolated-browser-bootstrap",
                "command": f"{command} auth spond browser-bootstrap",
            },
            {
                "source": "hoopit",
                "action": "isolated-browser-bootstrap",
                "command": f"{command} auth hoopit browser-bootstrap",
            },
            {
                "source": "ist-home",
                "action": "manual-login",
                "command": f"{command} auth ist-home login",
            },
        ],
    }


def onboard_from_bytes(
    root: Path | str | None,
    raw_bundle: bytes,
    *,
    trusted_keys: Mapping[str, Any] | None = None,
    now: datetime | None = None,
    command: str = DEFAULT_COMMAND,
) -> dict[str, Any]:
    bundle = verify_provider_bundle(raw_bundle, trusted_keys=trusted_keys, now=now)
    return provision_provider_bundle(root, bundle, now=now, command=command)


def onboard_from_file(
    root: Path | str | None,
    bundle_file: Path | str,
    *,
    trusted_keys: Mapping[str, Any] | None = None,
    now: datetime | None = None,
    command: str = DEFAULT_COMMAND,
) -> dict[str, Any]:
    try:
        raw = read_private_bytes(
            Path(bundle_file).expanduser(),
            max_bytes=MAX_BUNDLE_BYTES,
            missing_message="Fant ikke den private providerpakken",
        )
    except PrivateStateError as exc:
        raise ProviderBundleError(str(exc)) from None
    return onboard_from_bytes(root, raw, trusted_keys=trusted_keys, now=now, command=command)
