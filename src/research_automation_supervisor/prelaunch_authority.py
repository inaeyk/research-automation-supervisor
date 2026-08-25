"""Transactional Start and snapshot authority owned only by the Core service.

Filesystem objects are immutable supporting blobs.  A Start exists exclusively
when one row is committed in ``authority.sqlite3``; directory entries, partial
objects, Custodian cards, and snapshot staging content have no Start authority.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
import stat
import sys
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal, TypeVar, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from research_automation_supervisor.core_authority_models import (
    CampaignLaunchReferenceV1,
    CampaignLaunchRequestV1,
    CampaignLaunchSummaryV1,
    QualifiedLaunchMaterialV1,
    RequestedRepositoryAuthorityV1,
)
from research_automation_supervisor.custodian_errors import (
    QualifiedCampaignInputError,
    QualifiedCampaignStateError,
)
from research_automation_supervisor.custodian_models import (
    CampaignInputBundleV1,
    CampaignProfileSettingsV1,
    FrozenInputFileV1,
    RepositoryAuthorityV1,
)
from research_automation_supervisor.durable_state import canonical_json, fsync_directory
from research_automation_supervisor.gitless_repository import (
    SanitizedSnapshotPlanV1,
    build_sanitized_snapshot,
    freeze_repository_import,
    initialize_snapshot_store,
    load_gitless_import,
    materialize_campaign_workspace,
    plan_sanitized_snapshot,
    publish_workspace_verification_key,
    repository_authority_for_plan,
    verify_campaign_workspace,
    verify_sanitized_snapshot,
)

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
CrashInjector = Callable[[str], None]
ModelT = TypeVar("ModelT", bound=BaseModel)
DATABASE_NAME = "authority.sqlite3"
MAX_AUTHORITY_BYTES = 64 * 1024 * 1024


class CampaignLaunchIntentV1(BaseModel):
    """Immutable Start object referenced by a committed database row."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[2] = 2
    campaign_public_id: Annotated[
        str, Field(min_length=12, max_length=80, pattern=r"^campaign-[a-z0-9-]+$")
    ]
    start_request_sha256: Sha256
    preview_id: str
    client_start_key_sha256: Sha256
    human_name: str
    repository: RequestedRepositoryAuthorityV1
    research_contract: FrozenInputFileV1
    research_plan: FrozenInputFileV1
    initial_task: FrozenInputFileV1
    supporting_files: tuple[FrozenInputFileV1, ...] = ()
    requested_settings: CampaignProfileSettingsV1
    intent_sha256: Sha256

    @model_validator(mode="after")
    def validate_identity(self) -> CampaignLaunchIntentV1:
        if self.intent_sha256 != _self_hash(self, "intent_sha256"):
            raise ValueError("launch intent self-hash is invalid")
        return self


class FrozenCampaignInputV1(BaseModel):
    """Canonical bundle whose identity is bound by the Start transaction."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[2] = 2
    launch_intent_sha256: Sha256
    repository_preparation_sha256: Sha256
    input_bundle: CampaignInputBundleV1
    frozen_input_sha256: Sha256

    @model_validator(mode="after")
    def validate_identity(self) -> FrozenCampaignInputV1:
        if self.frozen_input_sha256 != _self_hash(self, "frozen_input_sha256"):
            raise ValueError("frozen campaign input self-hash is invalid")
        return self


def initialize_authority_store(authority_root: Path, snapshot_root: Path) -> None:
    """Create SQLite and storage with crash-authority durability settings."""
    root = _authority_root(authority_root)
    snapshots = initialize_snapshot_store(snapshot_root)
    secret = _store_secret(root)
    publish_workspace_verification_key(snapshots, secret)
    with _connect(root) as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS authority_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            ) WITHOUT ROWID;

            CREATE TABLE IF NOT EXISTS snapshots (
                snapshot_id TEXT PRIMARY KEY CHECK(length(snapshot_id) = 64),
                import_id TEXT NOT NULL CHECK(length(import_id) = 64),
                source_commit TEXT NOT NULL CHECK(length(source_commit) = 40),
                source_tree TEXT NOT NULL CHECK(length(source_tree) = 40),
                prepared_commit TEXT NOT NULL CHECK(length(prepared_commit) = 40),
                prepared_tree TEXT NOT NULL CHECK(length(prepared_tree) = 40),
                plan_json BLOB NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('absent', 'building', 'complete')),
                completed_at TEXT,
                CHECK((state = 'complete') = (completed_at IS NOT NULL))
            );

            CREATE TABLE IF NOT EXISTS starts (
                start_intent_id TEXT PRIMARY KEY,
                immutable_start_request_id TEXT NOT NULL UNIQUE
                    CHECK(length(immutable_start_request_id) = 64),
                canonical_request_sha256 TEXT NOT NULL CHECK(length(canonical_request_sha256) = 64),
                campaign_public_id TEXT NOT NULL UNIQUE,
                operator_uid INTEGER NOT NULL,
                operator_gid INTEGER,
                preview_id TEXT NOT NULL UNIQUE,
                repository_input_sha256 TEXT NOT NULL CHECK(length(repository_input_sha256) = 64),
                source_kind TEXT NOT NULL CHECK(source_kind IN ('existing_folder', 'git_url')),
                source_display TEXT NOT NULL,
                source_locator_sha256 TEXT NOT NULL CHECK(length(source_locator_sha256) = 64),
                source_commit TEXT NOT NULL CHECK(length(source_commit) = 40),
                source_tree TEXT NOT NULL CHECK(length(source_tree) = 40),
                contract_sha256 TEXT NOT NULL CHECK(length(contract_sha256) = 64),
                plan_sha256 TEXT NOT NULL CHECK(length(plan_sha256) = 64),
                task_sha256 TEXT NOT NULL CHECK(length(task_sha256) = 64),
                supporting_manifest_sha256 TEXT NOT NULL
                    CHECK(length(supporting_manifest_sha256) = 64),
                settings_sha256 TEXT NOT NULL CHECK(length(settings_sha256) = 64),
                input_bundle_sha256 TEXT NOT NULL UNIQUE CHECK(length(input_bundle_sha256) = 64),
                frozen_input_sha256 TEXT NOT NULL UNIQUE CHECK(length(frozen_input_sha256) = 64),
                intent_sha256 TEXT NOT NULL UNIQUE CHECK(length(intent_sha256) = 64),
                creation_transaction_id TEXT NOT NULL UNIQUE,
                import_id TEXT NOT NULL CHECK(length(import_id) = 64),
                expected_snapshot_id TEXT NOT NULL CHECK(length(expected_snapshot_id) = 64),
                current_snapshot_id TEXT,
                request_object_sha256 TEXT NOT NULL CHECK(length(request_object_sha256) = 64),
                intent_object_sha256 TEXT NOT NULL CHECK(length(intent_object_sha256) = 64),
                frozen_object_sha256 TEXT NOT NULL CHECK(length(frozen_object_sha256) = 64),
                created_at TEXT NOT NULL,
                FOREIGN KEY(expected_snapshot_id) REFERENCES snapshots(snapshot_id),
                FOREIGN KEY(current_snapshot_id) REFERENCES snapshots(snapshot_id),
                CHECK(current_snapshot_id IS NULL OR current_snapshot_id = expected_snapshot_id)
            );

            CREATE INDEX IF NOT EXISTS starts_created_at
                ON starts(created_at, campaign_public_id);
            """
        )
        existing = connection.execute(
            "SELECT value FROM authority_meta WHERE key = 'snapshot_root'"
        ).fetchone()
        if existing is None:
            connection.execute(
                "INSERT INTO authority_meta(key, value) VALUES('snapshot_root', ?)",
                (str(snapshots),),
            )
        elif existing[0] != str(snapshots):
            raise QualifiedCampaignStateError("Core snapshot storage identity changed")
        connection.commit()
    _secure_database_files(root)
    fsync_directory(root)


def create_start_intent(
    request: CampaignLaunchRequestV1,
    authority_root: Path,
    snapshot_root: Path,
    *,
    operator_uid: int | None = None,
    operator_gid: int | None = None,
    repository_descriptor: int | None = None,
    repository_transfer_descriptor: int | None = None,
    repository_bundle_descriptor: int | None = None,
    require_repository_descriptor: bool = False,
    now: datetime | None = None,
    crash_injector: CrashInjector | None = None,
) -> CampaignLaunchReferenceV1:
    """Commit Start once, then independently advance its snapshot transaction."""
    inject = crash_injector or (lambda _boundary: None)
    inject("before_input_object_creation")
    inject("before_core_transaction")  # PA-5C4-S compatibility boundary.
    initialize_authority_store(authority_root, snapshot_root)
    root = _authority_root(authority_root)
    snapshots = initialize_snapshot_store(snapshot_root)
    secret = _read_store_secret(root)
    request_sha = request.canonical_sha256()
    existing = _row_for_request_identity(root, request.client_start_key_sha256)
    if existing is not None:
        _require_same_request(existing, request_sha)
        _ensure_snapshot(root, snapshots, existing, inject)
        refreshed = _row_for_intent(root, str(existing["start_intent_id"]))
        return _reference_from_row(root, refreshed, secret)

    descriptor = repository_descriptor
    if descriptor is None:
        descriptor = repository_bundle_descriptor
    if (
        require_repository_descriptor
        and request.repository.source_kind == "existing_folder"
        and descriptor is None
        and repository_transfer_descriptor is None
    ):
        raise QualifiedCampaignInputError("new Start requires the selected repository object")

    imported = freeze_repository_import(
        request.repository,
        import_root=snapshots / "imports",
        repository_descriptor=(
            descriptor if repository_transfer_descriptor is None else None
        ),
        repository_transfer_descriptor=repository_transfer_descriptor,
        crash_injector=inject,
    )
    snapshot_plan = plan_sanitized_snapshot(
        imported, python_executable=sys.executable
    )
    campaign_mac = hmac.new(
        secret,
        f"campaign:{request.client_start_key_sha256}".encode("ascii"),
        "sha256",
    ).hexdigest()
    campaign_id = f"campaign-{campaign_mac[:24]}"
    intent_payload = {
        "schema_version": 2,
        "campaign_public_id": campaign_id,
        "start_request_sha256": request_sha,
        **request.model_dump(mode="python", exclude={"schema_version"}),
    }
    intent_sha = hashlib.sha256(canonical_json(intent_payload)).hexdigest()
    intent = CampaignLaunchIntentV1.model_validate(
        {**intent_payload, "intent_sha256": intent_sha}
    )
    intent_mac = hmac.new(secret, f"intent:{intent_sha}".encode("ascii"), "sha256").hexdigest()
    intent_id = f"intent_{intent_sha}_{intent_mac}"
    repository = repository_authority_for_plan(
        request.repository,
        snapshot_plan,
        snapshot_root=snapshots,
        campaign_public_id=campaign_id,
    )
    bundle = CampaignInputBundleV1.freeze(
        campaign_public_id=campaign_id,
        human_name=request.human_name,
        repository=repository,
        research_contract=request.research_contract,
        research_plan=request.research_plan,
        initial_task=request.initial_task,
        supporting_files=request.supporting_files,
        requested_settings=request.requested_settings,
    )
    frozen_payload = {
        "schema_version": 2,
        "launch_intent_sha256": intent_sha,
        "repository_preparation_sha256": snapshot_plan.snapshot_id,
        "input_bundle": bundle.model_dump(mode="python"),
    }
    frozen = FrozenCampaignInputV1.model_validate(
        {
            **frozen_payload,
            "frozen_input_sha256": hashlib.sha256(canonical_json(frozen_payload)).hexdigest(),
        }
    )
    request_bytes = canonical_json(request.model_dump(mode="json"))
    intent_bytes = canonical_json(intent.model_dump(mode="json"))
    frozen_bytes = canonical_json(frozen.model_dump(mode="json"))
    request_object_sha = _publish_object(root, "requests", request_sha, request_bytes, inject)
    intent_object_sha = _publish_object(root, "intents", intent_sha, intent_bytes)
    frozen_object_sha = _publish_object(
        root, "frozen-inputs", frozen.frozen_input_sha256, frozen_bytes
    )
    inject("after_object_fsync_before_db_transaction")
    inject("after_durable_objects_before_receipt")
    created_at = (now or datetime.now(UTC)).astimezone(UTC).isoformat().replace("+00:00", "Z")
    transaction_id = f"tx_{secrets.token_hex(32)}"
    repository_input_sha = hashlib.sha256(
        canonical_json(request.repository.model_dump(mode="json"))
    ).hexdigest()
    supporting_sha = hashlib.sha256(
        canonical_json([item.model_dump(mode="json") for item in request.supporting_files])
    ).hexdigest()
    settings_sha = hashlib.sha256(
        canonical_json(request.requested_settings.model_dump(mode="json"))
    ).hexdigest()
    plan_json = canonical_json(snapshot_plan.model_dump(mode="json"))
    uid = os.getuid() if operator_uid is None else operator_uid

    with _connect(root) as connection:
        connection.execute("BEGIN IMMEDIATE")
        conflict = connection.execute(
            "SELECT * FROM starts WHERE immutable_start_request_id = ? OR preview_id = ?",
            (request.client_start_key_sha256, request.preview_id),
        ).fetchone()
        if conflict is not None:
            _require_same_request(conflict, request_sha)
            connection.rollback()
            committed = cast(sqlite3.Row, conflict)
        else:
            inject("during_start_transaction")
            connection.execute(
                """
                INSERT OR IGNORE INTO snapshots(
                    snapshot_id, import_id, source_commit, source_tree,
                    prepared_commit, prepared_tree, plan_json, state, completed_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, 'absent', NULL)
                """,
                (
                    snapshot_plan.snapshot_id,
                    imported.import_id,
                    imported.source_commit,
                    imported.source_tree,
                    snapshot_plan.prepared_commit,
                    snapshot_plan.prepared_tree,
                    plan_json,
                ),
            )
            connection.execute(
                """
                INSERT INTO starts(
                    start_intent_id, immutable_start_request_id, canonical_request_sha256,
                    campaign_public_id, operator_uid, operator_gid, preview_id,
                    repository_input_sha256, source_kind, source_display,
                    source_locator_sha256, source_commit, source_tree,
                    contract_sha256, plan_sha256, task_sha256,
                    supporting_manifest_sha256, settings_sha256,
                    input_bundle_sha256, frozen_input_sha256, intent_sha256,
                    creation_transaction_id, import_id, expected_snapshot_id,
                    current_snapshot_id, request_object_sha256, intent_object_sha256,
                    frozen_object_sha256, created_at
                ) VALUES(
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    NULL, ?, ?, ?, ?
                )
                """,
                (
                    intent_id,
                    request.client_start_key_sha256,
                    request_sha,
                    campaign_id,
                    uid,
                    operator_gid,
                    request.preview_id,
                    repository_input_sha,
                    request.repository.source_kind,
                    request.repository.source_display,
                    request.repository.source_locator_sha256,
                    imported.source_commit,
                    imported.source_tree,
                    request.research_contract.sha256,
                    request.research_plan.sha256,
                    request.initial_task.sha256,
                    supporting_sha,
                    settings_sha,
                    bundle.bundle_sha256,
                    frozen.frozen_input_sha256,
                    intent_sha,
                    transaction_id,
                    imported.import_id,
                    snapshot_plan.snapshot_id,
                    request_object_sha,
                    intent_object_sha,
                    frozen_object_sha,
                    created_at,
                ),
            )
            inject("immediately_before_commit")
            connection.commit()
            committed = _row_for_intent_connection(connection, intent_id)
    _secure_database_files(root)
    inject("immediately_after_commit_before_response")
    inject("after_receipt_before_response")
    _ensure_snapshot(root, snapshots, committed, inject)
    refreshed = _row_for_intent(root, intent_id)
    return _reference_from_row(root, refreshed, secret)


def get_start_intent(authority_root: Path, launch_intent_id: str) -> CampaignLaunchSummaryV1:
    root = _existing_authority_root(authority_root)
    row = _row_for_intent(root, launch_intent_id)
    _verify_intent_mac(row, launch_intent_id, _read_store_secret(root))
    snapshot_state = _snapshot_state(root, str(row["expected_snapshot_id"]))
    return CampaignLaunchSummaryV1(
        campaign_public_id=str(row["campaign_public_id"]),
        preview_id=str(row["preview_id"]),
        launch_intent_id=launch_intent_id,
        launch_intent_sha256=str(row["intent_sha256"]),
        input_bundle_sha256=str(row["input_bundle_sha256"]),
        human_name=_load_intent(root, row).human_name,
        repository_display=str(row["source_display"]),
        created_at=str(row["created_at"]),
        snapshot_state=snapshot_state,
        snapshot_identity=(
            str(row["current_snapshot_id"])
            if row["current_snapshot_id"] is not None
            else None
        ),
    )


def list_operator_campaigns(authority_root: Path) -> tuple[CampaignLaunchSummaryV1, ...]:
    root = _existing_authority_root(authority_root)
    with _connect(root) as connection:
        rows = connection.execute(
            "SELECT start_intent_id FROM starts ORDER BY created_at, campaign_public_id"
        ).fetchall()
    return tuple(get_start_intent(root, str(row[0])) for row in rows)


def verify_start_intent(
    authority_root: Path,
    launch_intent_id: str,
    *,
    expected_campaign_public_id: str,
    expected_intent_sha256: str | None = None,
    expected_bundle_sha256: str | None = None,
) -> CampaignLaunchSummaryV1:
    summary = get_start_intent(authority_root, launch_intent_id)
    if summary.campaign_public_id != expected_campaign_public_id:
        raise QualifiedCampaignInputError("launch intent belongs to another campaign")
    if (
        expected_intent_sha256 is not None
        and summary.launch_intent_sha256 != expected_intent_sha256
    ):
        raise QualifiedCampaignInputError("launch intent identity was substituted")
    if expected_bundle_sha256 is not None and summary.input_bundle_sha256 != expected_bundle_sha256:
        raise QualifiedCampaignInputError("launch intent bundle was substituted")
    return summary


def consume_start_intent_for_qualified_launch(
    authority_root: Path,
    launch_intent_id: str,
    *,
    expected_campaign_public_id: str,
) -> QualifiedLaunchMaterialV1:
    root = _existing_authority_root(authority_root)
    row = _row_for_intent(root, launch_intent_id)
    _verify_intent_mac(row, launch_intent_id, _read_store_secret(root))
    if str(row["campaign_public_id"]) != expected_campaign_public_id:
        raise QualifiedCampaignInputError("launch intent belongs to another campaign")
    if row["current_snapshot_id"] != row["expected_snapshot_id"]:
        raise QualifiedCampaignStateError("sanitized repository snapshot is incomplete")
    frozen = _load_frozen(root, row)
    plan = _snapshot_plan(root, str(row["expected_snapshot_id"]))
    verify_campaign_workspace(
        Path(frozen.input_bundle.repository.prepared_workspace),
        campaign_public_id=str(row["campaign_public_id"]),
        launch_intent_id=launch_intent_id,
        launch_intent_sha256=str(row["intent_sha256"]),
        bundle_sha256=frozen.input_bundle.bundle_sha256,
        snapshot_id=plan.snapshot_id,
        baseline_commit=plan.prepared_commit,
        baseline_tree=plan.prepared_tree,
    )
    return QualifiedLaunchMaterialV1(
        campaign_public_id=str(row["campaign_public_id"]),
        launch_intent_id=launch_intent_id,
        launch_intent_sha256=str(row["intent_sha256"]),
        frozen_input_sha256=frozen.frozen_input_sha256,
        input_bundle=frozen.input_bundle,
        snapshot_identity=plan.snapshot_id,
    )


def resume_start_snapshot(
    authority_root: Path,
    snapshot_root: Path,
    launch_intent_id: str,
    *,
    crash_injector: CrashInjector | None = None,
) -> CampaignLaunchSummaryV1:
    """Deterministically resume an absent/building snapshot from frozen objects."""
    root = _existing_authority_root(authority_root)
    snapshots = initialize_snapshot_store(snapshot_root)
    row = _row_for_intent(root, launch_intent_id)
    _ensure_snapshot(root, snapshots, row, crash_injector or (lambda _boundary: None))
    return get_start_intent(root, launch_intent_id)


def freeze_launch_intent(
    request: CampaignLaunchRequestV1,
    authority_root: Path,
    *,
    now: datetime | None = None,
) -> CampaignLaunchReferenceV1:
    return create_start_intent(
        request,
        authority_root,
        authority_root.parent / "repository-snapshots",
        now=now,
    )


def load_launch_intent(
    authority_root: Path,
    launch_intent_id: str,
    *,
    expected_campaign_public_id: str | None = None,
    expected_intent_sha256: str | None = None,
) -> CampaignLaunchIntentV1:
    root = _existing_authority_root(authority_root)
    row = _row_for_intent(root, launch_intent_id)
    _verify_intent_mac(row, launch_intent_id, _read_store_secret(root))
    intent = _load_intent(root, row)
    if (
        expected_campaign_public_id is not None
        and intent.campaign_public_id != expected_campaign_public_id
    ):
        raise QualifiedCampaignInputError("launch intent belongs to another campaign")
    if expected_intent_sha256 is not None and intent.intent_sha256 != expected_intent_sha256:
        raise QualifiedCampaignInputError("launch intent identity was substituted")
    return intent


def load_launch_summary(
    authority_root: Path,
    launch_intent_id: str,
    *,
    expected_campaign_public_id: str,
    expected_intent_sha256: str,
) -> CampaignLaunchSummaryV1:
    return verify_start_intent(
        authority_root,
        launch_intent_id,
        expected_campaign_public_id=expected_campaign_public_id,
        expected_intent_sha256=expected_intent_sha256,
    )


def load_frozen_campaign_input(
    authority_root: Path, intent: CampaignLaunchIntentV1
) -> FrozenCampaignInputV1 | None:
    root = _existing_authority_root(authority_root)
    with _connect(root) as connection:
        row = connection.execute(
            "SELECT * FROM starts WHERE intent_sha256 = ?", (intent.intent_sha256,)
        ).fetchone()
    return None if row is None else _load_frozen(root, row)


def seal_frozen_campaign_input(
    authority_root: Path,
    intent: CampaignLaunchIntentV1,
    repository: RepositoryAuthorityV1,
    repository_preparation_sha256: str,
) -> FrozenCampaignInputV1:
    del authority_root, intent, repository, repository_preparation_sha256
    raise QualifiedCampaignInputError("frozen campaign input is committed only by atomic Start")


def authority_schema(authority_root: Path) -> tuple[str, ...]:
    """Return the deployed schema for mechanical qualification evidence."""
    root = _existing_authority_root(authority_root)
    with _connect(root) as connection:
        rows = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type IN ('table', 'index') "
            "AND sql IS NOT NULL ORDER BY name"
        ).fetchall()
    return tuple(str(row[0]) for row in rows)


def authoritative_start_count(authority_root: Path) -> int:
    """Count Starts solely from committed SQLite authority."""
    if not (authority_root / DATABASE_NAME).is_file():
        return 0
    root = _existing_authority_root(authority_root)
    with _connect(root) as connection:
        return int(connection.execute("SELECT count(*) FROM starts").fetchone()[0])


def _ensure_snapshot(
    root: Path,
    snapshots: Path,
    row: sqlite3.Row,
    inject: CrashInjector,
) -> None:
    snapshot_id = str(row["expected_snapshot_id"])
    plan = _snapshot_plan(root, snapshot_id)
    imported = load_gitless_import(snapshots / "imports" / str(row["import_id"]))
    with _connect(root) as connection:
        state = str(
            connection.execute(
                "SELECT state FROM snapshots WHERE snapshot_id = ?", (snapshot_id,)
            ).fetchone()[0]
        )
        if state == "complete":
            snapshot = snapshots / "complete" / snapshot_id
            verify_sanitized_snapshot(snapshot, plan)
            if row["current_snapshot_id"] is None:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "UPDATE starts SET current_snapshot_id = expected_snapshot_id "
                    "WHERE start_intent_id = ? AND current_snapshot_id IS NULL",
                    (str(row["start_intent_id"]),),
                )
                connection.commit()
                _secure_database_files(root)
        else:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE snapshots SET state = 'building' "
                "WHERE snapshot_id = ? AND state != 'complete'",
                (snapshot_id,),
            )
            connection.commit()
            snapshot = build_sanitized_snapshot(
                imported,
                plan,
                snapshot_root=snapshots,
                python_executable=sys.executable,
                crash_injector=inject,
            )
            completed_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
            with _connect(root) as commit_connection:
                commit_connection.execute("BEGIN IMMEDIATE")
                commit_connection.execute(
                    "UPDATE snapshots SET state = 'complete', completed_at = ? "
                    "WHERE snapshot_id = ?",
                    (completed_at, snapshot_id),
                )
                commit_connection.execute(
                    "UPDATE starts SET current_snapshot_id = expected_snapshot_id "
                    "WHERE start_intent_id = ?",
                    (str(row["start_intent_id"]),),
                )
                commit_connection.commit()
            _secure_database_files(root)
            inject("after_snapshot_commit_before_campaign_launch")
    frozen = _load_frozen(root, row)
    materialize_campaign_workspace(
        snapshot,
        plan,
        snapshot_root=snapshots,
        campaign_public_id=str(row["campaign_public_id"]),
        launch_intent_id=str(row["start_intent_id"]),
        launch_intent_sha256=str(row["intent_sha256"]),
        bundle_sha256=frozen.input_bundle.bundle_sha256,
        signing_secret=_read_store_secret(root),
        operator_uid=int(row["operator_uid"]),
        operator_gid=(int(row["operator_gid"]) if row["operator_gid"] is not None else None),
    )


def _reference_from_row(
    root: Path, row: sqlite3.Row, secret: bytes
) -> CampaignLaunchReferenceV1:
    launch_intent_id = str(row["start_intent_id"])
    _verify_intent_mac(row, launch_intent_id, secret)
    frozen = _load_frozen(root, row)
    if frozen.input_bundle.bundle_sha256 != row["input_bundle_sha256"]:
        raise QualifiedCampaignStateError("transactional Start bundle is corrupt")
    return CampaignLaunchReferenceV1(
        campaign_public_id=str(row["campaign_public_id"]),
        launch_intent_id=launch_intent_id,
        launch_intent_sha256=str(row["intent_sha256"]),
        input_bundle_sha256=str(row["input_bundle_sha256"]),
        snapshot_identity=(
            str(row["current_snapshot_id"])
            if row["current_snapshot_id"] is not None
            else None
        ),
    )


def _load_intent(root: Path, row: sqlite3.Row) -> CampaignLaunchIntentV1:
    content = _load_object(
        root, "intents", str(row["intent_object_sha256"]), "launch intent"
    )
    try:
        intent = CampaignLaunchIntentV1.model_validate_json(content)
    except ValidationError as exc:
        raise QualifiedCampaignStateError("launch intent object is invalid") from exc
    if intent.intent_sha256 != row["intent_sha256"]:
        raise QualifiedCampaignStateError("launch intent object was substituted")
    return intent


def _load_frozen(root: Path, row: sqlite3.Row) -> FrozenCampaignInputV1:
    content = _load_object(
        root, "frozen-inputs", str(row["frozen_object_sha256"]), "frozen input"
    )
    try:
        frozen = FrozenCampaignInputV1.model_validate_json(content)
    except ValidationError as exc:
        raise QualifiedCampaignStateError("frozen input object is invalid") from exc
    if (
        frozen.frozen_input_sha256 != row["frozen_input_sha256"]
        or frozen.launch_intent_sha256 != row["intent_sha256"]
        or frozen.input_bundle.bundle_sha256 != row["input_bundle_sha256"]
    ):
        raise QualifiedCampaignStateError("frozen input object was substituted")
    return frozen


def _snapshot_plan(root: Path, snapshot_id: str) -> SanitizedSnapshotPlanV1:
    with _connect(root) as connection:
        row = connection.execute(
            "SELECT plan_json FROM snapshots WHERE snapshot_id = ?", (snapshot_id,)
        ).fetchone()
    if row is None:
        raise QualifiedCampaignStateError("snapshot transaction is missing")
    try:
        return SanitizedSnapshotPlanV1.model_validate_json(bytes(row[0]))
    except ValidationError as exc:
        raise QualifiedCampaignStateError("snapshot transaction is corrupt") from exc


def _snapshot_state(
    root: Path, snapshot_id: str
) -> Literal["absent", "building", "complete"]:
    with _connect(root) as connection:
        row = connection.execute(
            "SELECT state FROM snapshots WHERE snapshot_id = ?", (snapshot_id,)
        ).fetchone()
    if row is None or row[0] not in {"absent", "building", "complete"}:
        raise QualifiedCampaignStateError("snapshot transaction is corrupt")
    return cast(Literal["absent", "building", "complete"], row[0])


def _row_for_request_identity(root: Path, request_id: str) -> sqlite3.Row | None:
    with _connect(root) as connection:
        row = connection.execute(
            "SELECT * FROM starts WHERE immutable_start_request_id = ?", (request_id,)
        ).fetchone()
    return cast(sqlite3.Row | None, row)


def _row_for_intent(root: Path, launch_intent_id: str) -> sqlite3.Row:
    with _connect(root) as connection:
        return _row_for_intent_connection(connection, launch_intent_id)


def _row_for_intent_connection(
    connection: sqlite3.Connection, launch_intent_id: str
) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM starts WHERE start_intent_id = ?", (launch_intent_id,)
    ).fetchone()
    if row is None:
        raise QualifiedCampaignInputError("launch intent is stale or invalid")
    return cast(sqlite3.Row, row)


def _require_same_request(row: sqlite3.Row, request_sha: str) -> None:
    if not hmac.compare_digest(str(row["canonical_request_sha256"]), request_sha):
        raise QualifiedCampaignInputError(
            "Start request identity was already bound to different fields"
        )


def _verify_intent_mac(row: sqlite3.Row, launch_intent_id: str, secret: bytes) -> None:
    intent_sha = str(row["intent_sha256"])
    mac = hmac.new(
        secret, f"intent:{intent_sha}".encode("ascii"), "sha256"
    ).hexdigest()
    expected = f"intent_{intent_sha}_{mac}"
    if not hmac.compare_digest(expected, launch_intent_id):
        raise QualifiedCampaignInputError("launch intent is stale or invalid")


def _self_hash(value: BaseModel, field: str) -> str:
    return hashlib.sha256(
        canonical_json(value.model_dump(mode="json", exclude={field}))
    ).hexdigest()


def _connect(root: Path) -> sqlite3.Connection:
    database = root / DATABASE_NAME
    connection = sqlite3.connect(database, timeout=30, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=FULL")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=30000")
    return connection


def _authority_root(path: Path) -> Path:
    try:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        absolute = Path(os.path.abspath(path))
        resolved = path.resolve(strict=True)
        if absolute != resolved:
            raise OSError("authority root resolves elsewhere")
        _validate_directory(resolved, "Core authority root")
        os.chmod(resolved, 0o700)
        for name in ("requests", "intents", "frozen-inputs"):
            child = resolved / name
            child.mkdir(exist_ok=True, mode=0o700)
            _validate_directory(child, "Core authority object directory")
            os.chmod(child, 0o700)
        fsync_directory(resolved)
        return resolved
    except (OSError, RuntimeError, ValueError) as exc:
        raise QualifiedCampaignStateError("Core authority storage is unavailable") from exc


def _existing_authority_root(path: Path) -> Path:
    try:
        absolute = Path(os.path.abspath(path))
        resolved = path.resolve(strict=True)
        if absolute != resolved:
            raise OSError("authority root resolves elsewhere")
        _validate_directory(resolved, "Core authority root")
        return resolved
    except (OSError, RuntimeError, ValueError) as exc:
        raise QualifiedCampaignInputError("Core authority storage is unavailable") from exc


def _validate_directory(path: Path, label: str) -> None:
    status = path.lstat()
    if stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode):
        raise OSError(f"{label} is unsafe")


def _store_secret(root: Path) -> bytes:
    path = root / "store-key-v1"
    if path.exists():
        return _read_store_secret(root)
    content = secrets.token_bytes(32)
    _write_new_regular(path, content, mode=0o600)
    fsync_directory(root)
    return content


def _read_store_secret(root: Path) -> bytes:
    content = _read_regular(root / "store-key-v1", "Core authority key", max_bytes=32)
    if len(content) != 32:
        raise QualifiedCampaignStateError("Core authority key is invalid")
    return content


def _publish_object(
    root: Path,
    kind: str,
    identity: str,
    content: bytes,
    inject: CrashInjector | None = None,
) -> str:
    digest = hashlib.sha256(content).hexdigest()
    del identity
    kind_directory = root / kind
    directory = kind_directory / digest[:2]
    created_shard = False
    try:
        directory.mkdir(mode=0o700)
        created_shard = True
    except FileExistsError:
        pass
    _validate_directory(directory, "Core object shard")
    if created_shard:
        fsync_directory(kind_directory)
    path = directory / f"{digest}.json"
    if path.exists():
        observed = _read_regular(path, "immutable object", max_bytes=MAX_AUTHORITY_BYTES)
        if not hmac.compare_digest(observed, content):
            raise QualifiedCampaignStateError("immutable object identity collided")
        return digest
    descriptor, temporary_name = tempfile.mkstemp(dir=directory, prefix=".object-")
    temporary = Path(temporary_name)
    try:
        _write_all(descriptor, content)
        if inject is not None:
            inject("during_input_object_creation")
        os.fchmod(descriptor, 0o400)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.link(temporary, path, follow_symlinks=False)
        fsync_directory(directory)
    except FileExistsError:
        observed = _read_regular(path, "immutable object", max_bytes=MAX_AUTHORITY_BYTES)
        if not hmac.compare_digest(observed, content):
            raise QualifiedCampaignStateError(
                "immutable object identity collided"
            ) from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
    return digest


def _load_object(root: Path, kind: str, digest: str, label: str) -> bytes:
    content = _read_regular(
        root / kind / digest[:2] / f"{digest}.json",
        label,
        max_bytes=MAX_AUTHORITY_BYTES,
    )
    if not hmac.compare_digest(hashlib.sha256(content).hexdigest(), digest):
        raise QualifiedCampaignStateError(f"{label} hash is invalid")
    return content


def _write_new_regular(path: Path, content: bytes, *, mode: int) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        mode,
    )
    try:
        _write_all(descriptor, content)
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_all(descriptor: int, content: bytes) -> None:
    offset = 0
    while offset < len(content):
        written = os.write(descriptor, content[offset:])
        if written <= 0:
            raise OSError("short Core authority write")
        offset += written


def _read_regular(path: Path, label: str, *, max_bytes: int) -> bytes:
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            status = os.fstat(descriptor)
            if not stat.S_ISREG(status.st_mode) or status.st_size > max_bytes:
                raise OSError("unsafe immutable object")
            chunks: list[bytes] = []
            remaining = max_bytes + 1
            while remaining:
                chunk = os.read(descriptor, min(1024 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            content = b"".join(chunks)
            if len(content) > max_bytes:
                raise OSError("immutable object exceeds limit")
            return content
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise QualifiedCampaignStateError(f"{label} is unavailable or unsafe") from exc


def _secure_database_files(root: Path) -> None:
    for name in (DATABASE_NAME, f"{DATABASE_NAME}-wal", f"{DATABASE_NAME}-shm"):
        path = root / name
        if path.exists():
            status = path.lstat()
            if stat.S_ISLNK(status.st_mode) or not stat.S_ISREG(status.st_mode):
                raise QualifiedCampaignStateError("Core transaction database is unsafe")
            os.chmod(path, 0o600)
