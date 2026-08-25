"""Non-executing repository intake and sanitized snapshot construction.

This module is the complete pre-snapshot repository implementation.  It never
uses :mod:`subprocess`, a shell, Git configuration, hooks, attributes, or
credential helpers.  Local repositories are first copied through retained
directory descriptors into a core-owned bare object store.  HTTPS repositories
are fetched by Dulwich with an empty configuration.  All later construction is
performed from that core-owned object store.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import io
import json
import os
import re
import shutil
import stat
import struct
import tempfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast
from urllib.parse import urlsplit

import urllib3
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from dulwich import porcelain
from dulwich.client import get_transport_and_path
from dulwich.config import ConfigFile
from dulwich.index import build_index_from_tree
from dulwich.object_store import iter_tree_contents
from dulwich.objects import Blob, Commit, ObjectID, Tree
from dulwich.refs import Ref
from dulwich.repo import Repo
from pydantic import BaseModel, ConfigDict, Field, model_validator

from research_automation_supervisor.core_authority_models import (
    RequestedRepositoryAuthorityV1,
)
from research_automation_supervisor.custodian_errors import (
    QualifiedCampaignInputError,
    QualifiedCampaignStateError,
)
from research_automation_supervisor.custodian_models import (
    RepositoryAuthorityV1,
    render_qualified_acceptance_runner,
)
from research_automation_supervisor.durable_state import canonical_json, fsync_directory

CrashInjector = Callable[[str], None]
_SHA1 = re.compile(r"^[0-9a-f]{40}$")
_COPY_CHUNK = 1024 * 1024
_IMPORT_SCHEMA = 1
_SNAPSHOT_SCHEMA = 1
_TRANSFER_MAGIC = b"RAS-GITLESS-TRANSFER-V1\n"
_TRANSFER_HEADER = struct.Struct(">IQ")
_TRANSFER_MAX_PATH = 4096
_TRANSFER_MAX_FILE = 16 * 1024 * 1024 * 1024
_TRANSFER_MAX_TOTAL = 256 * 1024 * 1024 * 1024
_GIT_POINTER_MAX_FILE = 16 * 1024
_WORKSPACE_BINDING_DOMAIN = "research-supervisor-workspace-binding-v1"
_WORKSPACE_PUBLIC_KEY = "workspace-verification-key-v1"
_PRODUCTION_SNAPSHOT_ROOT = Path("/var/lib/research-supervisor-core/snapshots")
_TRUSTED_CONFIG = (
    b"[core]\n"
    b"\trepositoryformatversion = 0\n"
    b"\tfilemode = true\n"
    b"\tbare = false\n"
    b"\tlogallrefupdates = true\n"
    b"\thooksPath = /dev/null\n"
    b"\tfsmonitor = false\n"
    b"[credential]\n\thelper =\n"
    b'[protocol "ext"]\n\tallow = never\n'
    b'[protocol "file"]\n\tallow = never\n'
    b'[protocol "ssh"]\n\tallow = never\n'
    b'[protocol "git"]\n\tallow = never\n'
)


class GitlessImportV1(BaseModel):
    """Identity of one immutable, core-owned Git object import."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    import_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_kind: Literal["existing_folder", "git_url"]
    source_locator_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    source_tree: str = Field(pattern=r"^[0-9a-f]{40}$")
    object_store_path: str
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_identity(self) -> GitlessImportV1:
        payload = self.model_dump(
            mode="json", exclude={"import_id", "object_store_path", "manifest_sha256"}
        )
        expected = hashlib.sha256(canonical_json(payload)).hexdigest()
        if self.import_id != expected:
            raise ValueError("repository import identity is invalid")
        return self


class SanitizedSnapshotPlanV1(BaseModel):
    """Deterministic snapshot identity known before Start is committed."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    snapshot_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    import_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    source_tree: str = Field(pattern=r"^[0-9a-f]{40}$")
    prepared_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    prepared_tree: str = Field(pattern=r"^[0-9a-f]{40}$")
    acceptance_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    acceptance_content_base64: str

    @model_validator(mode="after")
    def validate_snapshot_identity(self) -> SanitizedSnapshotPlanV1:
        try:
            acceptance = base64.b64decode(self.acceptance_content_base64, validate=True)
        except ValueError as exc:
            raise ValueError("sanitized acceptance content is invalid") from exc
        if hashlib.sha256(acceptance).hexdigest() != self.acceptance_sha256:
            raise ValueError("sanitized acceptance content hash is invalid")
        expected = hashlib.sha256(
            canonical_json(self.model_dump(mode="json", exclude={"snapshot_id"}))
        ).hexdigest()
        if self.snapshot_id != expected:
            raise ValueError("sanitized snapshot identity is invalid")
        return self


@dataclass(frozen=True)
class _PreparedObjects:
    root_tree: Tree
    commit: Commit
    acceptance_blob: Blob
    acceptance_tree: Tree


@dataclass(frozen=True)
class _LinkedWorktreeMetadata:
    git_file_fd: int
    admin_fd: int
    common_fd: int
    git_file_identity: os.stat_result
    admin_identity: os.stat_result
    common_identity: os.stat_result
    gitdir_pointer: str
    admin_back_pointer: str
    commondir_pointer: str


@dataclass(frozen=True)
class _LocalGitMetadata:
    repository_fd: int
    repository_identity: os.stat_result
    head_fd: int
    common_fd: int
    control_identity: os.stat_result
    control_kind: Literal["bare", "directory", "linked_worktree"]
    owned_fds: tuple[int, ...]
    linked: _LinkedWorktreeMetadata | None = None


def inspect_requested_repository(
    source_kind: Literal["existing_folder", "git_url"],
    locator: str,
    *,
    sterile_root: Path,
    repository_descriptor: int | None = None,
    repository_transfer_descriptor: int | None = None,
    source_device: int | None = None,
    source_inode: int | None = None,
) -> RequestedRepositoryAuthorityV1:
    """Read HEAD identity without consulting repository-controlled behavior."""
    locator = locator.strip()
    sterile = _private_root(sterile_root)
    descriptor: int | None = None
    owns_descriptor = False
    try:
        if source_kind == "existing_folder":
            if repository_descriptor is None and repository_transfer_descriptor is None:
                locator, descriptor = open_repository_directory(locator)
                owns_descriptor = True
            elif repository_descriptor is not None:
                descriptor = repository_descriptor
            identity = os.fstat(descriptor) if descriptor is not None else None
            if identity is not None:
                if not stat.S_ISDIR(identity.st_mode):
                    raise QualifiedCampaignInputError("repository folder is unsafe")
                if source_device is not None and identity.st_dev != source_device:
                    raise QualifiedCampaignInputError(
                        "repository folder changed during inspection"
                    )
                if source_inode is not None and identity.st_ino != source_inode:
                    raise QualifiedCampaignInputError(
                        "repository folder changed during inspection"
                    )
            with tempfile.TemporaryDirectory(dir=sterile, prefix="inspect-") as name:
                bare = Path(name) / "objects.git"
                if repository_transfer_descriptor is not None:
                    _read_repository_transfer(repository_transfer_descriptor, bare)
                elif descriptor is not None:
                    _copy_local_git_metadata(descriptor, bare)
                else:  # pragma: no cover - guarded above
                    raise QualifiedCampaignInputError("repository transfer is missing")
                commit, tree = _read_head_identity(bare)
            display = Path(locator).name
            requested_tree: str | None = tree
            source_device = identity.st_dev if identity is not None else source_device
            source_inode = identity.st_ino if identity is not None else source_inode
        else:
            display = validate_https_url(locator)
            try:
                refs = _https_remote_refs(locator)
            except QualifiedCampaignInputError:
                raise
            except Exception as exc:
                raise QualifiedCampaignInputError(
                    "HTTPS repository could not be inspected safely"
                ) from exc
            head = refs.get(cast(Ref, b"HEAD"))
            commit = head.decode("ascii") if isinstance(head, bytes) else ""
            if _SHA1.fullmatch(commit) is None:
                raise QualifiedCampaignInputError("HTTPS repository HEAD could not be identified")
            requested_tree = None
            source_device = None
            source_inode = None
        locator_hash = hashlib.sha256(locator.encode("utf-8")).hexdigest()
        repository_name = Path(display).name or "repository"
        cleaned = re.sub(r"[^A-Za-z0-9_-]+", "-", repository_name).strip("-")
        cleaned = cleaned or "repository"
        return RequestedRepositoryAuthorityV1(
            source_kind=source_kind,
            source_display=display,
            source_locator=locator,
            source_locator_sha256=locator_hash,
            requested_commit=commit,
            requested_tree=requested_tree,
            source_device=source_device,
            source_inode=source_inode,
            repository_id=f"{cleaned[:60]}-{locator_hash[:12]}"[:80],
        )
    except (OSError, KeyError, UnicodeError) as exc:
        raise QualifiedCampaignInputError("repository could not be inspected safely") from exc
    finally:
        if owns_descriptor and descriptor is not None:
            os.close(descriptor)


def freeze_repository_import(
    requested: RequestedRepositoryAuthorityV1,
    *,
    import_root: Path,
    repository_descriptor: int | None = None,
    repository_transfer_descriptor: int | None = None,
    crash_injector: CrashInjector | None = None,
) -> GitlessImportV1:
    """Freeze source objects before Start, without making them authoritative."""
    inject = crash_injector or (lambda _boundary: None)
    root = _private_root(import_root)
    temporary = Path(tempfile.mkdtemp(dir=root, prefix=".import-"))
    bare = temporary / "objects.git"
    descriptor: int | None = None
    owns_descriptor = False
    try:
        if requested.source_kind == "existing_folder":
            if repository_transfer_descriptor is not None:
                _read_repository_transfer(repository_transfer_descriptor, bare)
            elif repository_descriptor is None:
                _, descriptor = open_repository_directory(requested.source_locator)
                owns_descriptor = True
            else:
                descriptor = repository_descriptor
            if repository_transfer_descriptor is None:
                if descriptor is None:  # pragma: no cover - guarded above
                    raise QualifiedCampaignInputError("repository transfer is missing")
                identity = os.fstat(descriptor)
                if (
                    identity.st_dev != requested.source_device
                    or identity.st_ino != requested.source_inode
                ):
                    raise QualifiedCampaignInputError("repository changed after preview")
                _copy_local_git_metadata(descriptor, bare)
        else:
            validate_https_url(requested.source_locator)
            try:
                porcelain.clone(
                    requested.source_locator,
                    bare,
                    bare=True,
                    checkout=False,
                    origin=None,
                    config=ConfigFile(),
                    recurse_submodules=False,
                    env={},
                    errstream=io.BytesIO(),
                    pool_manager=_HttpsOnlyPoolManager(  # type: ignore[arg-type]
                        requested.source_locator
                    ),
                )
            except Exception as exc:
                raise QualifiedCampaignInputError(
                    "HTTPS repository could not be imported safely"
                ) from exc
            _replace_file(bare / "config", _trusted_bare_config())
        inject("during_input_object_creation")
        commit, tree = _read_head_identity(bare)
        if commit != requested.requested_commit:
            raise QualifiedCampaignInputError("repository changed after preview; review it again")
        if requested.requested_tree is not None and tree != requested.requested_tree:
            raise QualifiedCampaignInputError(
                "repository tree changed after preview; review it again"
            )
        manifest_sha = _manifest_sha256(bare)
        identity_payload = {
            "schema_version": _IMPORT_SCHEMA,
            "source_kind": requested.source_kind,
            "source_locator_sha256": requested.source_locator_sha256,
            "source_commit": commit,
            "source_tree": tree,
        }
        import_id = hashlib.sha256(canonical_json(identity_payload)).hexdigest()
        metadata = GitlessImportV1.model_validate(
            {
                **identity_payload,
                "import_id": import_id,
                "object_store_path": str(root / import_id / "objects.git"),
                "manifest_sha256": manifest_sha,
            }
        )
        _write_new_file(
            temporary / "import-v1.json",
            canonical_json(metadata.model_dump(mode="json")),
            mode=0o400,
        )
        _fsync_tree(temporary)
        target = root / import_id
        if target.exists():
            existing = load_gitless_import(target)
            logical_fields = (
                "schema_version",
                "import_id",
                "source_kind",
                "source_locator_sha256",
                "source_commit",
                "source_tree",
                "object_store_path",
            )
            if any(
                getattr(existing, field) != getattr(metadata, field)
                for field in logical_fields
            ):
                raise QualifiedCampaignStateError("repository import identity collided")
            shutil.rmtree(temporary)
            metadata = existing
        else:
            os.replace(temporary, target)
            fsync_directory(root)
            _make_tree_read_only(target)
            _fsync_tree(target)
        return metadata
    except BaseException:
        if temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)
        raise
    finally:
        if owns_descriptor and descriptor is not None:
            os.close(descriptor)


def load_gitless_import(path: Path) -> GitlessImportV1:
    """Load and verify a committed Start's referenced raw object import."""
    try:
        value = GitlessImportV1.model_validate_json(
            _read_regular(path / "import-v1.json", max_bytes=64 * 1024)
        )
        if Path(value.object_store_path) != path / "objects.git":
            raise ValueError("object-store path mismatch")
        commit, tree = _read_head_identity(path / "objects.git")
        if commit != value.source_commit or tree != value.source_tree:
            raise ValueError("object-store identity mismatch")
        if _manifest_sha256(path / "objects.git") != value.manifest_sha256:
            raise ValueError("object-store manifest mismatch")
        return value
    except (OSError, ValueError) as exc:
        raise QualifiedCampaignStateError("repository import object is invalid") from exc


def plan_sanitized_snapshot(
    imported: GitlessImportV1, *, python_executable: str
) -> SanitizedSnapshotPlanV1:
    """Compute the complete sanitized commit and snapshot identities."""
    source = Repo(imported.object_store_path)
    acceptance = render_qualified_acceptance_runner(python_executable)
    prepared = _prepared_objects(source, imported.source_commit, acceptance)
    payload = {
        "schema_version": _SNAPSHOT_SCHEMA,
        "import_id": imported.import_id,
        "source_commit": imported.source_commit,
        "source_tree": imported.source_tree,
        "prepared_commit": prepared.commit.id.decode("ascii"),
        "prepared_tree": prepared.root_tree.id.decode("ascii"),
        "acceptance_sha256": hashlib.sha256(prepared.acceptance_blob.data).hexdigest(),
        "acceptance_content_base64": base64.b64encode(acceptance).decode("ascii"),
    }
    return SanitizedSnapshotPlanV1.model_validate(
        {
            **payload,
            "snapshot_id": hashlib.sha256(canonical_json(payload)).hexdigest(),
        }
    )


def repository_authority_for_plan(
    requested: RequestedRepositoryAuthorityV1,
    plan: SanitizedSnapshotPlanV1,
    *,
    snapshot_root: Path,
    campaign_public_id: str,
) -> RepositoryAuthorityV1:
    workspace = snapshot_root / "workspaces" / campaign_public_id / "repository"
    return RepositoryAuthorityV1(
        source_kind=requested.source_kind,
        source_display=requested.source_display,
        source_locator_sha256=requested.source_locator_sha256,
        prepared_workspace=str(workspace),
        baseline_commit=plan.prepared_commit,
        baseline_tree=plan.prepared_tree,
        repository_id=requested.repository_id,
    )


def build_sanitized_snapshot(
    imported: GitlessImportV1,
    plan: SanitizedSnapshotPlanV1,
    *,
    snapshot_root: Path,
    python_executable: str,
    crash_injector: CrashInjector | None = None,
) -> Path:
    """Build and atomically finalize core-owned immutable snapshot content."""
    del python_executable
    inject = crash_injector or (lambda _boundary: None)
    root = _snapshot_storage_root(snapshot_root)
    complete_root = root / "complete"
    staging_root = root / "staging"
    target = complete_root / plan.snapshot_id
    if target.exists():
        verify_sanitized_snapshot(target, plan)
        return target
    staging = Path(tempfile.mkdtemp(dir=staging_root, prefix=f"{plan.snapshot_id}."))
    try:
        inject("during_repository_snapshot_staging")
        workspace = staging / "repository"
        repo = Repo.init(workspace, mkdir=True, default_branch=b"sanitized")
        source = Repo(imported.object_store_path)
        acceptance = base64.b64decode(plan.acceptance_content_base64, validate=True)
        prepared = _prepared_objects(source, imported.source_commit, acceptance)
        _copy_reachable_history(source, repo, imported.source_commit)
        for item in (
            prepared.acceptance_blob,
            prepared.acceptance_tree,
            prepared.root_tree,
            prepared.commit,
        ):
            repo.object_store.add_object(item)
        sanitized_ref = cast(Ref, b"refs/heads/sanitized")
        repo.refs[sanitized_ref] = prepared.commit.id
        repo.refs.set_symbolic_ref(cast(Ref, b"HEAD"), sanitized_ref)
        build_index_from_tree(
            str(workspace),
            str(repo.index_path()),
            repo.object_store,
            prepared.root_tree.id,
            blob_normalizer=None,
        )
        _replace_file(Path(repo.controldir()) / "config", _TRUSTED_CONFIG)
        hooks = Path(repo.controldir()) / "hooks"
        if hooks.exists():
            shutil.rmtree(hooks)
        metadata = {
            **plan.model_dump(mode="json"),
            "reader": "dulwich-object-reader-v1",
            "source_configuration_copied": False,
            "source_attributes_executed": False,
        }
        _write_new_file(
            staging / "snapshot-v1.json",
            canonical_json(metadata),
            mode=0o400,
        )
        _fsync_tree(staging)
        try:
            os.replace(staging, target)
            fsync_directory(complete_root)
        except FileExistsError:
            shutil.rmtree(staging)
        _make_tree_read_only(target)
        _fsync_tree(target)
        verify_sanitized_snapshot(target, plan)
        inject("after_snapshot_content_before_snapshot_db_commit")
        return target
    except BaseException:
        if staging.exists():
            # A building directory is deliberately non-authoritative and may be
            # removed by the process that created it.
            shutil.rmtree(staging, ignore_errors=True)
        raise


def verify_sanitized_snapshot(path: Path, plan: SanitizedSnapshotPlanV1) -> None:
    """Verify immutable content without accepting pathname presence as authority."""
    try:
        metadata = json.loads(_read_regular(path / "snapshot-v1.json", max_bytes=128 * 1024))
        for key, expected in plan.model_dump(mode="json").items():
            if metadata.get(key) != expected:
                raise ValueError("snapshot metadata mismatch")
        repo = Repo(str(path / "repository"))
        head = repo.head().decode("ascii")
        commit = cast(Commit, repo[repo.head()])
        if head != plan.prepared_commit or commit.tree.decode("ascii") != plan.prepared_tree:
            raise ValueError("snapshot Git identity mismatch")
        config = _read_regular(Path(repo.controldir()) / "config", max_bytes=64 * 1024)
        if config != _TRUSTED_CONFIG:
            raise ValueError("snapshot configuration mismatch")
        _verify_worktree_matches_tree(path / "repository", repo, commit.tree)
    except (OSError, KeyError, UnicodeError, ValueError) as exc:
        raise QualifiedCampaignStateError("sanitized snapshot is invalid") from exc


def materialize_campaign_workspace(
    snapshot: Path,
    plan: SanitizedSnapshotPlanV1,
    *,
    snapshot_root: Path,
    campaign_public_id: str,
    launch_intent_id: str,
    launch_intent_sha256: str,
    bundle_sha256: str,
    signing_secret: bytes,
    operator_uid: int | None,
    operator_gid: int | None,
) -> Path:
    """Create the sole mutable repository used after snapshot completion."""
    del operator_uid  # Delegation is group-only; Core retains every path's UID.
    verify_sanitized_snapshot(snapshot, plan)
    root = _snapshot_storage_root(snapshot_root)
    workspaces = root / "workspaces"
    workspace_root_status = workspaces.lstat()
    if operator_gid is not None and workspace_root_status.st_gid != operator_gid:
        raise QualifiedCampaignStateError("workspace shared-group authority changed")
    campaign = workspaces / campaign_public_id
    repository = campaign / "repository"
    binding = campaign / "snapshot-binding-v1.json"
    expected_binding = _signed_workspace_binding(
        signing_secret,
        workspace=repository,
        campaign_public_id=campaign_public_id,
        launch_intent_id=launch_intent_id,
        launch_intent_sha256=launch_intent_sha256,
        bundle_sha256=bundle_sha256,
        snapshot_id=plan.snapshot_id,
        baseline_commit=plan.prepared_commit,
        baseline_tree=plan.prepared_tree,
    )
    if campaign.exists():
        if _read_regular(binding, max_bytes=64 * 1024) != expected_binding:
            raise QualifiedCampaignStateError("campaign workspace binding is invalid")
        _verify_workspace_identity(repository, plan)
        _verify_trusted_control_directory(repository)
        return repository
    temporary = Path(tempfile.mkdtemp(dir=workspaces, prefix=f".{campaign_public_id}."))
    try:
        temporary_status = temporary.lstat()
        if (
            stat.S_IMODE(temporary_status.st_mode) != 0o2700
            or temporary_status.st_uid != workspace_root_status.st_uid
            or temporary_status.st_gid != workspace_root_status.st_gid
        ):
            raise QualifiedCampaignStateError(
                "workspace staging did not inherit the installer-owned shared group"
            )
        previous_umask = os.umask(0o007)
        try:
            staged_campaign = temporary / "campaign"
            staged_campaign.mkdir(mode=0o1770)
        finally:
            os.umask(previous_umask)
        staged_status = staged_campaign.lstat()
        if (
            stat.S_IMODE(staged_status.st_mode) != 0o3770
            or staged_status.st_uid != workspace_root_status.st_uid
            or staged_status.st_gid != workspace_root_status.st_gid
        ):
            raise QualifiedCampaignStateError(
                "campaign root did not inherit setgid authority"
            )
        staged_repository = staged_campaign / "repository"
        _copy_mutable_workspace(snapshot / "repository", staged_repository)
        _verify_workspace_identity(staged_repository, plan)
        _verify_trusted_control_directory(staged_repository)
        _write_new_file(
            staged_campaign / "snapshot-binding-v1.json", expected_binding, mode=0o440
        )
        _fsync_tree(staged_campaign)
        os.replace(staged_campaign, campaign)
        fsync_directory(workspaces)
        temporary.rmdir()
        _verify_workspace_identity(repository, plan)
        _verify_trusted_control_directory(repository)
        return repository
    except BaseException:
        if temporary.exists():
            _remove_workspace_staging(temporary, workspaces)
        raise


def verify_campaign_workspace(
    workspace: Path,
    *,
    campaign_public_id: str,
    launch_intent_id: str,
    launch_intent_sha256: str,
    bundle_sha256: str,
    snapshot_id: str,
    baseline_commit: str,
    baseline_tree: str,
) -> None:
    """Prove a post-snapshot path was created from committed core authority."""
    binding = verify_operator_campaign_workspace(
        workspace,
        trusted_snapshot_root=workspace.parents[2],
    )
    if binding != {
        "authority_domain": _WORKSPACE_BINDING_DOMAIN,
        "schema_version": 1,
        "workspace_path": str(workspace),
        "campaign_public_id": campaign_public_id,
        "launch_intent_id": launch_intent_id,
        "launch_intent_sha256": launch_intent_sha256,
        "bundle_sha256": bundle_sha256,
        "snapshot_id": snapshot_id,
        "baseline_commit": baseline_commit,
        "baseline_tree": baseline_tree,
        "signature_base64": binding.get("signature_base64"),
    }:
        raise QualifiedCampaignInputError("prepared repository snapshot binding is invalid")
    metadata = json.loads(
        _read_regular(
            workspace.parents[2] / "complete" / snapshot_id / "snapshot-v1.json",
            max_bytes=128 * 1024,
        )
    )
    plan = SanitizedSnapshotPlanV1.model_validate(
        {key: metadata[key] for key in SanitizedSnapshotPlanV1.model_fields}
    )
    if plan.snapshot_id != snapshot_id:
        raise QualifiedCampaignInputError("prepared repository snapshot identity is invalid")
    verify_sanitized_snapshot(
        workspace.parents[2] / "complete" / snapshot_id,
        plan,
    )
    _verify_workspace_identity(workspace, plan)
    _verify_trusted_control_directory(workspace)


def verify_operator_campaign_workspace(
    workspace: Path,
    *,
    trusted_snapshot_root: Path | None = None,
) -> dict[str, object]:
    """Verify a Core-signed capability without reading the private store."""
    try:
        root = (trusted_snapshot_root or _required_snapshot_root()).resolve(strict=True)
        resolved = workspace.resolve(strict=True)
        expected_campaign = root / "workspaces" / resolved.parent.name
        if resolved != expected_campaign / "repository":
            raise ValueError("workspace lies outside the trusted workspace root")
        campaign = resolved.parent
        binding = campaign / "snapshot-binding-v1.json"
        observed = _read_regular(binding, max_bytes=64 * 1024)
        value = json.loads(observed)
        required = {
            "authority_domain",
            "schema_version",
            "workspace_path",
            "campaign_public_id",
            "launch_intent_id",
            "launch_intent_sha256",
            "bundle_sha256",
            "snapshot_id",
            "baseline_commit",
            "baseline_tree",
            "signature_base64",
        }
        if (
            not isinstance(value, dict)
            or set(value) != required
            or value.get("authority_domain") != _WORKSPACE_BINDING_DOMAIN
            or value.get("schema_version") != 1
            or value.get("workspace_path") != str(resolved)
            or value.get("campaign_public_id") != campaign.name
            or re.fullmatch(
                r"intent_[0-9a-f]{64}_[0-9a-f]{64}",
                str(value.get("launch_intent_id")),
            )
            is None
            or any(
                re.fullmatch(pattern, str(value.get(key))) is None
                for key, pattern in (
                    ("launch_intent_sha256", r"[0-9a-f]{64}"),
                    ("bundle_sha256", r"[0-9a-f]{64}"),
                    ("snapshot_id", r"[0-9a-f]{64}"),
                    ("baseline_commit", r"[0-9a-f]{40}"),
                    ("baseline_tree", r"[0-9a-f]{40}"),
                )
            )
            or canonical_json(value) != observed
        ):
            raise ValueError("workspace binding payload is invalid")
        signature_text = value["signature_base64"]
        if not isinstance(signature_text, str):
            raise ValueError("workspace binding signature is invalid")
        signature = base64.b64decode(signature_text, validate=True)
        payload = {key: item for key, item in value.items() if key != "signature_base64"}
        public_key = Ed25519PublicKey.from_public_bytes(
            _read_regular(root / _WORKSPACE_PUBLIC_KEY, max_bytes=32)
        )
        public_key.verify(signature, canonical_json(payload))
        expected_uid = (root / _WORKSPACE_PUBLIC_KEY).lstat().st_uid
        if root == _PRODUCTION_SNAPSHOT_ROOT:
            _verify_production_trust_anchor(root, expected_uid)
        _verify_core_owned_workspace_capability(resolved, root, expected_uid)
        repo = Repo(str(resolved))
        commit = cast(Commit, repo[repo.head()])
        if (
            repo.head().decode("ascii") != value["baseline_commit"]
            or commit.tree.decode("ascii") != value["baseline_tree"]
        ):
            raise ValueError("workspace identity does not match its Core binding")
        _verify_trusted_control_directory(resolved)
        return value
    except (InvalidSignature, KeyError, OSError, UnicodeError, ValueError) as exc:
        raise QualifiedCampaignInputError(
            "prepared repository snapshot binding is invalid"
        ) from exc


def open_repository_directory(locator: str) -> tuple[str, int]:
    """Open every selected-root path component with no-follow semantics."""
    absolute = str(Path(os.path.abspath(locator)))
    path = Path(absolute)
    parts = path.parts[1:]
    if not parts:
        raise QualifiedCampaignInputError("filesystem root is not a repository selection")
    descriptor = os.open(
        "/",
        getattr(os, "O_PATH", os.O_RDONLY)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0),
    )
    try:
        for index, part in enumerate(parts):
            final = index == len(parts) - 1
            flags = (
                (os.O_RDONLY if final else getattr(os, "O_PATH", os.O_RDONLY))
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return absolute, descriptor
    except BaseException:
        os.close(descriptor)
        raise


def create_local_repository_transfer(repository_fd: int) -> int:
    """Serialize object/ref bytes under the selecting UID without executing code.

    The returned unlinked regular-file descriptor is safe to delegate over
    ``SCM_RIGHTS`` to a Core UID that cannot traverse a private source tree.
    """
    transfer_fd = -1
    try:
        with tempfile.TemporaryFile(prefix="ras-gitless-transfer-") as temporary:
            with _open_local_git_metadata(repository_fd) as metadata:
                _write_all(temporary.fileno(), _TRANSFER_MAGIC)
                _transfer_required_file(
                    metadata.head_fd, "HEAD", temporary.fileno(), ("HEAD",)
                )
                _transfer_directory(
                    metadata.common_fd,
                    "objects",
                    temporary.fileno(),
                    required=True,
                )
                _transfer_directory(
                    metadata.common_fd,
                    "refs",
                    temporary.fileno(),
                    required=False,
                )
                for name in ("packed-refs", "shallow"):
                    _transfer_required_file(
                        metadata.common_fd,
                        name,
                        temporary.fileno(),
                        (name,),
                        required=False,
                    )
            _write_all(temporary.fileno(), _TRANSFER_HEADER.pack(0, 0))
            os.fsync(temporary.fileno())
            os.lseek(temporary.fileno(), 0, os.SEEK_SET)
            transfer_fd = os.dup(temporary.fileno())
        return transfer_fd
    except (OSError, UnicodeError) as exc:
        if transfer_fd >= 0:
            os.close(transfer_fd)
        raise QualifiedCampaignInputError("repository metadata is unsafe") from exc
    except BaseException:
        if transfer_fd >= 0:
            os.close(transfer_fd)
        raise


def validate_https_url(locator: str) -> str:
    """Accept only credential-free ordinary HTTPS repository URLs."""
    parsed = urlsplit(locator)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
        or bool(parsed.query)
        or bool(parsed.fragment)
        or not parsed.path
        or ".." in Path(parsed.path).parts
        or any(character.isspace() for character in locator)
    ):
        raise QualifiedCampaignInputError("only credential-free HTTPS Git URLs are supported")
    return f"{parsed.hostname}{parsed.path.removesuffix('.git')}"


class _HttpsOnlyPoolManager:
    """urllib3 policy that rejects redirects and origin/scheme changes."""

    def __init__(self, locator: str) -> None:
        parsed = urlsplit(locator)
        self._host = (parsed.hostname or "").casefold()
        self._pool = urllib3.PoolManager()
        self.headers = self._pool.headers

    def __getattr__(self, name: str) -> object:
        return getattr(self._pool, name)

    def request(self, method: str, url: str, *args: object, **kwargs: object) -> object:
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or (parsed.hostname or "").casefold() != self._host
            or parsed.port not in (None, 443)
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise QualifiedCampaignInputError("HTTPS repository transport changed origin")
        kwargs["redirect"] = False
        return self._pool.request(method, url, *args, **kwargs)  # type: ignore[arg-type]


def _https_remote_refs(locator: str) -> dict[Ref, ObjectID | None]:
    validate_https_url(locator)
    client, path = get_transport_and_path(
        locator,
        config=ConfigFile(),
        operation="fetch",
        pool_manager=_HttpsOnlyPoolManager(locator),  # type: ignore[arg-type]
    )
    return dict(client.get_refs(path.encode("utf-8")).refs)


def _transfer_directory(
    parent_fd: int, name: str, transfer_fd: int, *, required: bool
) -> None:
    try:
        source_fd = _open_checked_directory(parent_fd, name)
    except FileNotFoundError:
        if required:
            raise
        return
    try:
        _transfer_directory_contents(
            source_fd, transfer_fd, relative=(name,)
        )
    finally:
        os.close(source_fd)


def _transfer_directory_contents(
    source_fd: int, transfer_fd: int, *, relative: tuple[str, ...]
) -> None:
    before = os.fstat(source_fd)
    entries = sorted(os.listdir(source_fd))
    for name in entries:
        if not name or name in {".", ".."} or "/" in name or "\x00" in name:
            raise QualifiedCampaignInputError("repository metadata name is unsafe")
        if relative == ("objects", "info") and name in {
            "alternates",
            "http-alternates",
        }:
            continue
        status = os.stat(name, dir_fd=source_fd, follow_symlinks=False)
        if stat.S_ISDIR(status.st_mode):
            child_fd = _open_checked_directory(source_fd, name, expected=status)
            try:
                _transfer_directory_contents(
                    child_fd, transfer_fd, relative=(*relative, name)
                )
            finally:
                os.close(child_fd)
        elif stat.S_ISREG(status.st_mode):
            _transfer_required_file(
                source_fd,
                name,
                transfer_fd,
                (*relative, name),
                expected=status,
            )
        else:
            raise QualifiedCampaignInputError(
                "repository metadata contains a non-regular entry"
            )
    after = os.fstat(source_fd)
    if entries != sorted(os.listdir(source_fd)) or _changed_stat(before, after):
        raise QualifiedCampaignInputError("repository metadata changed during import")


def _transfer_required_file(
    parent_fd: int,
    name: str,
    transfer_fd: int,
    relative: tuple[str, ...],
    *,
    required: bool = True,
    expected: os.stat_result | None = None,
) -> None:
    try:
        observed = expected or os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        if required:
            raise
        return
    if not stat.S_ISREG(observed.st_mode) or observed.st_size > _TRANSFER_MAX_FILE:
        raise QualifiedCampaignInputError("repository metadata file is unsafe")
    source = os.open(
        name,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=parent_fd,
    )
    try:
        opened = os.fstat(source)
        if _changed_stat(observed, opened):
            raise QualifiedCampaignInputError("repository metadata file changed")
        path = "/".join(relative).encode("utf-8")
        if not path or len(path) > _TRANSFER_MAX_PATH:
            raise QualifiedCampaignInputError("repository metadata path is unsafe")
        _write_all(transfer_fd, _TRANSFER_HEADER.pack(len(path), opened.st_size))
        _write_all(transfer_fd, path)
        digest = hashlib.sha256()
        remaining = opened.st_size
        while remaining:
            chunk = os.read(source, min(_COPY_CHUNK, remaining))
            if not chunk:
                raise QualifiedCampaignInputError("repository metadata file was truncated")
            digest.update(chunk)
            _write_all(transfer_fd, chunk)
            remaining -= len(chunk)
        after = os.fstat(source)
        if _changed_stat(opened, after):
            raise QualifiedCampaignInputError(
                "repository metadata file changed while reading"
            )
        _write_all(transfer_fd, digest.digest())
    finally:
        os.close(source)


def _read_repository_transfer(transfer_fd: int, destination: Path) -> None:
    try:
        transfer_status = os.fstat(transfer_fd)
        if not stat.S_ISREG(transfer_status.st_mode):
            raise OSError("repository transfer is not regular")
        os.lseek(transfer_fd, 0, os.SEEK_SET)
        if _read_exact_fd(transfer_fd, len(_TRANSFER_MAGIC)) != _TRANSFER_MAGIC:
            raise OSError("repository transfer header is invalid")
        destination.mkdir(mode=0o700)
        seen: set[str] = set()
        total = 0
        while True:
            header = _read_exact_fd(transfer_fd, _TRANSFER_HEADER.size)
            path_length, content_length = _TRANSFER_HEADER.unpack(header)
            if path_length == 0:
                if content_length != 0:
                    raise OSError("repository transfer terminator is invalid")
                break
            if path_length > _TRANSFER_MAX_PATH or content_length > _TRANSFER_MAX_FILE:
                raise OSError("repository transfer record exceeds its limit")
            total += content_length
            if total > _TRANSFER_MAX_TOTAL:
                raise OSError("repository transfer exceeds its total limit")
            path_text = _read_exact_fd(transfer_fd, path_length).decode("utf-8")
            relative = _validated_transfer_path(path_text)
            if path_text in seen:
                raise OSError("repository transfer contains a duplicate path")
            seen.add(path_text)
            target = destination.joinpath(*relative)
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            output = os.open(
                target,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                0o400,
            )
            digest = hashlib.sha256()
            try:
                remaining = content_length
                while remaining:
                    chunk = _read_exact_fd(transfer_fd, min(_COPY_CHUNK, remaining))
                    digest.update(chunk)
                    _write_all(output, chunk)
                    remaining -= len(chunk)
                if not hmac.compare_digest(
                    digest.digest(), _read_exact_fd(transfer_fd, 32)
                ):
                    raise OSError("repository transfer content hash is invalid")
                os.fchmod(output, 0o400)
                os.fsync(output)
            finally:
                os.close(output)
        if "HEAD" not in seen or not any(path.startswith("objects/") for path in seen):
            raise OSError("repository transfer lacks required authority")
        if os.read(transfer_fd, 1):
            raise OSError("repository transfer has trailing data")
        _replace_file(destination / "config", _trusted_bare_config())
        _fsync_tree(destination)
    except (OSError, UnicodeError, ValueError) as exc:
        raise QualifiedCampaignInputError("repository transfer is unsafe") from exc


def _validated_transfer_path(value: str) -> tuple[str, ...]:
    parts = tuple(value.split("/"))
    if (
        not parts
        or any(
            not part
            or part in {".", ".."}
            or "\x00" in part
            or "\\" in part
            or any(ord(character) < 32 for character in part)
            for part in parts
        )
        or (
            parts not in {("HEAD",), ("packed-refs",), ("shallow",)}
            and parts[:1] not in {("objects",), ("refs",)}
        )
        or parts in {
            ("objects", "info", "alternates"),
            ("objects", "info", "http-alternates"),
        }
    ):
        raise OSError("repository transfer path is unsafe")
    return parts


def _open_checked_directory(
    parent_fd: int, name: str, *, expected: os.stat_result | None = None
) -> int:
    observed = expected or os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if not stat.S_ISDIR(observed.st_mode):
        raise QualifiedCampaignInputError("repository metadata directory is unsafe")
    descriptor = os.open(
        name,
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=parent_fd,
    )
    opened = os.fstat(descriptor)
    if _changed_stat(observed, opened):
        os.close(descriptor)
        raise QualifiedCampaignInputError("repository metadata directory changed")
    return descriptor


def _open_checked_regular(
    parent_fd: int,
    name: str,
    *,
    expected: os.stat_result | None = None,
    max_bytes: int | None = None,
) -> int:
    observed = expected or os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if (
        not stat.S_ISREG(observed.st_mode)
        or (max_bytes is not None and observed.st_size > max_bytes)
    ):
        raise QualifiedCampaignInputError("repository metadata file is unsafe")
    descriptor = os.open(
        name,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=parent_fd,
    )
    opened = os.fstat(descriptor)
    if _changed_stat(observed, opened):
        os.close(descriptor)
        raise QualifiedCampaignInputError("repository metadata file changed")
    return descriptor


def _same_object(before: os.stat_result, after: os.stat_result) -> bool:
    return (
        before.st_dev,
        before.st_ino,
        stat.S_IFMT(before.st_mode),
    ) == (
        after.st_dev,
        after.st_ino,
        stat.S_IFMT(after.st_mode),
    )


def _read_pointer_file(descriptor: int, *, prefix: bytes | None, label: str) -> str:
    before = os.fstat(descriptor)
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_size <= 0
        or before.st_size > _GIT_POINTER_MAX_FILE
    ):
        raise QualifiedCampaignInputError(f"{label} is unsafe")
    os.lseek(descriptor, 0, os.SEEK_SET)
    content = _read_exact_fd(descriptor, before.st_size)
    if os.read(descriptor, 1):
        raise QualifiedCampaignInputError(f"{label} changed while reading")
    after = os.fstat(descriptor)
    if _changed_stat(before, after):
        raise QualifiedCampaignInputError(f"{label} changed while reading")
    if b"\x00" in content:
        raise QualifiedCampaignInputError(f"{label} contains unsafe content")
    if content.endswith(b"\r\n"):
        line = content[:-2]
    elif content.endswith(b"\n"):
        line = content[:-1]
    else:
        line = content
    if b"\n" in line or b"\r" in line:
        raise QualifiedCampaignInputError(f"{label} contains multiple directives")
    if prefix is not None:
        if not line.startswith(prefix):
            raise QualifiedCampaignInputError(f"{label} has an invalid directive")
        line = line[len(prefix) :]
    if (
        not line
        or line[:1].isspace()
        or line[-1:].isspace()
        or any(byte < 32 or byte == 127 for byte in line)
    ):
        raise QualifiedCampaignInputError(f"{label} contains an unsafe path")
    return os.fsdecode(line)


def _pointer_parts(value: str) -> tuple[bool, tuple[str, ...]]:
    if not value or len(os.fsencode(value)) > _TRANSFER_MAX_PATH:
        raise QualifiedCampaignInputError("repository metadata pointer is unsafe")
    absolute = value.startswith("/")
    raw = value[1:] if absolute else value
    parts = tuple(raw.split("/"))
    if (
        not parts
        or any(
            not part
            or part == "."
            or "\x00" in part
            or any(ord(character) < 32 or ord(character) == 127 for character in part)
            for part in parts
        )
    ):
        raise QualifiedCampaignInputError("repository metadata pointer is unsafe")
    return absolute, parts


def _open_pointer_target(
    anchor_fd: int,
    value: str,
    *,
    target_kind: Literal["directory", "file"],
) -> int:
    absolute, parts = _pointer_parts(value)
    descriptor = os.open(
        "/",
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    ) if absolute else os.dup(anchor_fd)
    try:
        for part in parts[:-1]:
            child = _open_checked_directory(descriptor, part)
            os.close(descriptor)
            descriptor = child
        if target_kind == "directory":
            target = _open_checked_directory(descriptor, parts[-1])
        else:
            target = _open_checked_regular(
                descriptor,
                parts[-1],
                max_bytes=_GIT_POINTER_MAX_FILE,
            )
        os.close(descriptor)
        return target
    except BaseException:
        os.close(descriptor)
        raise


def _read_named_pointer(
    parent_fd: int, name: str, *, prefix: bytes | None, label: str
) -> str:
    descriptor = _open_checked_regular(
        parent_fd, name, max_bytes=_GIT_POINTER_MAX_FILE
    )
    try:
        return _read_pointer_file(descriptor, prefix=prefix, label=label)
    finally:
        os.close(descriptor)


def _require_same_object(
    descriptor: int, expected: os.stat_result, *, label: str
) -> None:
    if not _same_object(expected, os.fstat(descriptor)):
        raise QualifiedCampaignInputError(f"{label} does not identify the selected object")


def _verify_linked_worktree_topology(
    admin_fd: int, common_fd: int, *, admin_identity: os.stat_result
) -> None:
    with contextlib.ExitStack() as descriptors:
        admin_parent = _open_checked_directory(admin_fd, "..")
        descriptors.callback(os.close, admin_parent)
        common_worktrees = _open_checked_directory(common_fd, "worktrees")
        descriptors.callback(os.close, common_worktrees)
        common_from_admin = _open_checked_directory(admin_parent, "..")
        descriptors.callback(os.close, common_from_admin)
        if not _same_object(os.fstat(admin_parent), os.fstat(common_worktrees)):
            raise QualifiedCampaignInputError(
                "linked-worktree administrative topology is invalid"
            )
        if not _same_object(os.fstat(common_fd), os.fstat(common_from_admin)):
            raise QualifiedCampaignInputError("linked-worktree common topology is invalid")
        found_admin = False
        for name in os.listdir(common_worktrees):
            try:
                candidate_status = os.stat(
                    name, dir_fd=common_worktrees, follow_symlinks=False
                )
            except FileNotFoundError:
                continue
            if _same_object(admin_identity, candidate_status):
                candidate = _open_checked_directory(
                    common_worktrees, name, expected=candidate_status
                )
                os.close(candidate)
                found_admin = True
                break
        if not found_admin:
            raise QualifiedCampaignInputError(
                "linked-worktree administrative directory is not registered"
            )


def _open_linked_worktree_metadata(
    repository_fd: int,
    repository_identity: os.stat_result,
    marker: os.stat_result,
) -> _LocalGitMetadata:
    owned: list[int] = []
    try:
        git_file_fd = _open_checked_regular(
            repository_fd,
            ".git",
            expected=marker,
            max_bytes=_GIT_POINTER_MAX_FILE,
        )
        owned.append(git_file_fd)
        git_file_identity = os.fstat(git_file_fd)
        gitdir_pointer = _read_pointer_file(
            git_file_fd,
            prefix=b"gitdir: ",
            label="linked-worktree .git file",
        )
        admin_fd = _open_pointer_target(
            repository_fd, gitdir_pointer, target_kind="directory"
        )
        owned.append(admin_fd)
        admin_identity = os.fstat(admin_fd)
        admin_back_pointer = _read_named_pointer(
            admin_fd,
            "gitdir",
            prefix=None,
            label="linked-worktree admin gitdir back-reference",
        )
        reciprocal = _open_pointer_target(
            admin_fd, admin_back_pointer, target_kind="file"
        )
        try:
            _require_same_object(
                reciprocal,
                git_file_identity,
                label="linked-worktree admin gitdir back-reference",
            )
        finally:
            os.close(reciprocal)
        commondir_pointer = _read_named_pointer(
            admin_fd,
            "commondir",
            prefix=None,
            label="linked-worktree commondir",
        )
        common_fd = _open_pointer_target(
            admin_fd, commondir_pointer, target_kind="directory"
        )
        owned.append(common_fd)
        common_identity = os.fstat(common_fd)
        _verify_linked_worktree_topology(
            admin_fd, common_fd, admin_identity=admin_identity
        )
        common_head = _open_checked_regular(
            common_fd, "HEAD", max_bytes=_GIT_POINTER_MAX_FILE
        )
        os.close(common_head)
        objects = _open_checked_directory(common_fd, "objects")
        os.close(objects)
        linked = _LinkedWorktreeMetadata(
            git_file_fd=git_file_fd,
            admin_fd=admin_fd,
            common_fd=common_fd,
            git_file_identity=git_file_identity,
            admin_identity=admin_identity,
            common_identity=common_identity,
            gitdir_pointer=gitdir_pointer,
            admin_back_pointer=admin_back_pointer,
            commondir_pointer=commondir_pointer,
        )
        return _LocalGitMetadata(
            repository_fd=repository_fd,
            repository_identity=repository_identity,
            head_fd=admin_fd,
            common_fd=common_fd,
            control_identity=admin_identity,
            control_kind="linked_worktree",
            owned_fds=tuple(owned),
            linked=linked,
        )
    except BaseException:
        for descriptor in reversed(owned):
            os.close(descriptor)
        raise


def _open_local_git_metadata_value(repository_fd: int) -> _LocalGitMetadata:
    repository_identity = os.fstat(repository_fd)
    if not stat.S_ISDIR(repository_identity.st_mode):
        raise QualifiedCampaignInputError("repository folder is unsafe")
    try:
        marker = os.stat(".git", dir_fd=repository_fd, follow_symlinks=False)
    except FileNotFoundError:
        return _LocalGitMetadata(
            repository_fd=repository_fd,
            repository_identity=repository_identity,
            head_fd=repository_fd,
            common_fd=repository_fd,
            control_identity=repository_identity,
            control_kind="bare",
            owned_fds=(),
        )
    if stat.S_ISDIR(marker.st_mode):
        control_fd = _open_checked_directory(repository_fd, ".git", expected=marker)
        return _LocalGitMetadata(
            repository_fd=repository_fd,
            repository_identity=repository_identity,
            head_fd=control_fd,
            common_fd=control_fd,
            control_identity=os.fstat(control_fd),
            control_kind="directory",
            owned_fds=(control_fd,),
        )
    if stat.S_ISREG(marker.st_mode):
        return _open_linked_worktree_metadata(
            repository_fd, repository_identity, marker
        )
    raise QualifiedCampaignInputError("repository .git metadata is unsafe")


def _verify_local_git_metadata(metadata: _LocalGitMetadata) -> None:
    if not _same_object(metadata.repository_identity, os.fstat(metadata.repository_fd)):
        raise QualifiedCampaignInputError("repository changed during import")
    if metadata.control_kind == "bare":
        try:
            os.stat(".git", dir_fd=metadata.repository_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        raise QualifiedCampaignInputError("repository layout changed during import")
    if _changed_stat(metadata.control_identity, os.fstat(metadata.head_fd)):
        raise QualifiedCampaignInputError("repository metadata changed during import")
    if metadata.control_kind == "directory":
        reopened = _open_checked_directory(metadata.repository_fd, ".git")
        try:
            _require_same_object(
                reopened,
                metadata.control_identity,
                label="repository .git directory",
            )
        finally:
            os.close(reopened)
        return
    linked = metadata.linked
    if linked is None:  # pragma: no cover - model invariant
        raise QualifiedCampaignInputError("linked-worktree metadata is incomplete")
    if _changed_stat(linked.git_file_identity, os.fstat(linked.git_file_fd)):
        raise QualifiedCampaignInputError("linked-worktree .git file changed")
    if _changed_stat(linked.admin_identity, os.fstat(linked.admin_fd)):
        raise QualifiedCampaignInputError("linked-worktree admin directory changed")
    if _changed_stat(linked.common_identity, os.fstat(linked.common_fd)):
        raise QualifiedCampaignInputError("linked-worktree common directory changed")
    reopened_git_file = _open_checked_regular(
        metadata.repository_fd, ".git", max_bytes=_GIT_POINTER_MAX_FILE
    )
    try:
        _require_same_object(
            reopened_git_file,
            linked.git_file_identity,
            label="linked-worktree .git file",
        )
        if _read_pointer_file(
            reopened_git_file,
            prefix=b"gitdir: ",
            label="linked-worktree .git file",
        ) != linked.gitdir_pointer:
            raise QualifiedCampaignInputError("linked-worktree .git pointer changed")
    finally:
        os.close(reopened_git_file)
    reopened_admin = _open_pointer_target(
        metadata.repository_fd, linked.gitdir_pointer, target_kind="directory"
    )
    try:
        _require_same_object(
            reopened_admin,
            linked.admin_identity,
            label="linked-worktree administrative directory",
        )
    finally:
        os.close(reopened_admin)
    if _read_named_pointer(
        linked.admin_fd,
        "gitdir",
        prefix=None,
        label="linked-worktree admin gitdir back-reference",
    ) != linked.admin_back_pointer:
        raise QualifiedCampaignInputError("linked-worktree admin back-reference changed")
    reciprocal = _open_pointer_target(
        linked.admin_fd, linked.admin_back_pointer, target_kind="file"
    )
    try:
        _require_same_object(
            reciprocal,
            linked.git_file_identity,
            label="linked-worktree admin gitdir back-reference",
        )
    finally:
        os.close(reciprocal)
    if _read_named_pointer(
        linked.admin_fd,
        "commondir",
        prefix=None,
        label="linked-worktree commondir",
    ) != linked.commondir_pointer:
        raise QualifiedCampaignInputError("linked-worktree commondir changed")
    reopened_common = _open_pointer_target(
        linked.admin_fd, linked.commondir_pointer, target_kind="directory"
    )
    try:
        _require_same_object(
            reopened_common,
            linked.common_identity,
            label="linked-worktree common directory",
        )
    finally:
        os.close(reopened_common)
    _verify_linked_worktree_topology(
        linked.admin_fd,
        linked.common_fd,
        admin_identity=linked.admin_identity,
    )


@contextlib.contextmanager
def _open_local_git_metadata(
    repository_fd: int,
) -> Iterator[_LocalGitMetadata]:
    metadata = _open_local_git_metadata_value(repository_fd)
    try:
        yield metadata
        _verify_local_git_metadata(metadata)
    finally:
        for descriptor in reversed(metadata.owned_fds):
            os.close(descriptor)


def _changed_stat(before: os.stat_result, after: os.stat_result) -> bool:
    return (
        before.st_dev,
        before.st_ino,
        stat.S_IFMT(before.st_mode),
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    ) != (
        after.st_dev,
        after.st_ino,
        stat.S_IFMT(after.st_mode),
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )


def _read_exact_fd(descriptor: int, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = os.read(descriptor, min(_COPY_CHUNK, remaining))
        if not chunk:
            raise OSError("repository transfer is truncated")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _copy_local_git_metadata(repository_fd: int, destination: Path) -> None:
    """Copy only object/ref authority; source config and executables are ignored."""
    try:
        with _open_local_git_metadata(repository_fd) as metadata:
            destination.mkdir(mode=0o700)
            _copy_required_file(metadata.head_fd, "HEAD", destination / "HEAD")
            _copy_directory(
                metadata.common_fd,
                "objects",
                destination / "objects",
                required=True,
            )
            _copy_directory(
                metadata.common_fd,
                "refs",
                destination / "refs",
                required=False,
            )
            for name in ("packed-refs", "shallow"):
                _copy_required_file(
                    metadata.common_fd, name, destination / name, required=False
                )
            _replace_file(destination / "config", _trusted_bare_config())
            _fsync_tree(destination)
    except (OSError, UnicodeError) as exc:
        raise QualifiedCampaignInputError("repository metadata is unsafe") from exc


def _copy_directory(
    parent_fd: int, name: str, destination: Path, *, required: bool
) -> None:
    try:
        source_fd = os.open(
            name,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_fd,
        )
    except FileNotFoundError:
        if required:
            raise
        destination.mkdir(mode=0o700)
        return
    try:
        destination.mkdir(mode=0o700)
        _copy_directory_contents(source_fd, destination, relative=(name,))
        fsync_directory(destination)
    finally:
        os.close(source_fd)


def _copy_directory_contents(
    source_fd: int, destination: Path, *, relative: tuple[str, ...]
) -> None:
    before = os.fstat(source_fd)
    entries = sorted(os.listdir(source_fd))
    for name in entries:
        if not name or name in {".", ".."} or "/" in name or "\x00" in name:
            raise OSError("unsafe repository metadata name")
        if relative == ("objects", "info") and name in {"alternates", "http-alternates"}:
            # Alternates are pathname authority and are deliberately not imported.
            continue
        status = os.stat(name, dir_fd=source_fd, follow_symlinks=False)
        target = destination / name
        if stat.S_ISDIR(status.st_mode):
            child_fd = _open_checked_directory(source_fd, name, expected=status)
            try:
                target.mkdir(mode=0o700)
                _copy_directory_contents(
                    child_fd, target, relative=(*relative, name)
                )
                fsync_directory(target)
            finally:
                os.close(child_fd)
        elif stat.S_ISREG(status.st_mode):
            _copy_open_file(source_fd, name, target, status)
        else:
            raise OSError("repository metadata contains a non-regular entry")
    after = os.fstat(source_fd)
    if entries != sorted(os.listdir(source_fd)) or _changed_stat(before, after):
        raise OSError("repository metadata directory changed")


def _copy_required_file(
    parent_fd: int, name: str, destination: Path, *, required: bool = True
) -> None:
    try:
        status = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        if required:
            raise
        return
    if not stat.S_ISREG(status.st_mode):
        raise OSError("repository metadata file is unsafe")
    _copy_open_file(parent_fd, name, destination, status)


def _copy_open_file(
    parent_fd: int, name: str, destination: Path, expected: os.stat_result
) -> None:
    source = os.open(
        name,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=parent_fd,
    )
    destination_fd: int | None = None
    try:
        opened = os.fstat(source)
        if not stat.S_ISREG(opened.st_mode) or _changed_stat(expected, opened):
            raise OSError("repository metadata file changed")
        destination_fd = os.open(
            destination,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o400,
        )
        while True:
            chunk = os.read(source, _COPY_CHUNK)
            if not chunk:
                break
            _write_all(destination_fd, chunk)
        after = os.fstat(source)
        if _changed_stat(opened, after):
            raise OSError("repository metadata file changed while reading")
        os.fsync(destination_fd)
    finally:
        os.close(source)
        if destination_fd is not None:
            os.close(destination_fd)


def _read_head_identity(bare: Path) -> tuple[str, str]:
    try:
        repo = Repo(str(bare))
        head_id = repo.head()
        commit = repo[head_id]
        if not isinstance(commit, Commit):
            raise ValueError("HEAD is not a commit")
        head = head_id.decode("ascii")
        tree = commit.tree.decode("ascii")
        if _SHA1.fullmatch(head) is None or _SHA1.fullmatch(tree) is None:
            raise ValueError("unsupported object format")
        return head, tree
    except (OSError, KeyError, UnicodeError, ValueError) as exc:
        raise QualifiedCampaignInputError("repository HEAD could not be read safely") from exc


def _prepared_objects(
    source: Repo, source_commit: str, acceptance_content: bytes
) -> _PreparedObjects:
    source_id = cast(ObjectID, source_commit.encode("ascii"))
    source_object = source[source_id]
    if not isinstance(source_object, Commit):
        raise QualifiedCampaignInputError("repository HEAD is not a commit")
    _validate_reachable_history(source, source_id)
    source_tree = source[source_object.tree]
    if not isinstance(source_tree, Tree):
        raise QualifiedCampaignInputError("repository HEAD tree is invalid")
    if b".research-supervisor" in source_tree:
        raise QualifiedCampaignInputError("repository uses a reserved trusted-system path")
    acceptance_blob = Blob.from_string(acceptance_content)
    acceptance_tree = Tree()
    acceptance_tree.add(b"acceptance.py", 0o100500, acceptance_blob.id)
    root_tree = Tree()
    for name, mode, object_id in source_tree.iteritems():
        root_tree.add(name, mode, object_id)
    root_tree.add(b".research-supervisor", 0o040000, acceptance_tree.id)
    commit = Commit()
    commit.tree = root_tree.id
    commit.parents = [source_id]
    commit.author = b"Research Supervisor Core <core@localhost.invalid>"
    commit.committer = commit.author
    commit.author_time = 0
    commit.commit_time = 0
    commit.author_timezone = 0
    commit.commit_timezone = 0
    commit.message = b"chore: construct sanitized research snapshot\n"
    return _PreparedObjects(root_tree, commit, acceptance_blob, acceptance_tree)


def _validate_reachable_history(source: Repo, start: ObjectID) -> None:
    pending: list[ObjectID] = [start]
    seen: set[ObjectID] = set()
    while pending:
        object_id = pending.pop()
        if object_id in seen:
            continue
        seen.add(object_id)
        value = source[object_id]
        if isinstance(value, Commit):
            pending.append(value.tree)
            pending.extend(value.parents)
        elif isinstance(value, Tree):
            for name, mode, child in value.iteritems():
                _validate_tree_name(name)
                if stat.S_ISLNK(mode) or mode == 0o160000:
                    raise QualifiedCampaignInputError(
                        "repository history contains unsupported links or submodules"
                    )
                pending.append(child)
        elif not isinstance(value, Blob):
            raise QualifiedCampaignInputError("repository history contains an invalid object")


def _validate_tree_name(name: bytes) -> None:
    try:
        text = name.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise QualifiedCampaignInputError("repository path is not safe UTF-8") from exc
    if (
        not text
        or text in {".", ".."}
        or "/" in text
        or "\x00" in text
        or text.casefold() == ".git"
        or any(ord(character) < 32 for character in text)
    ):
        raise QualifiedCampaignInputError("repository contains an unsafe path")


def _copy_reachable_history(source: Repo, target: Repo, start: str) -> None:
    pending: list[ObjectID] = [cast(ObjectID, start.encode("ascii"))]
    seen: set[ObjectID] = set()
    while pending:
        object_id = pending.pop()
        if object_id in seen:
            continue
        seen.add(object_id)
        value = source[object_id]
        target.object_store.add_object(value)
        if isinstance(value, Commit):
            pending.append(value.tree)
            pending.extend(value.parents)
        elif isinstance(value, Tree):
            pending.extend(child for _name, _mode, child in value.iteritems())


def _verify_workspace_identity(repository: Path, plan: SanitizedSnapshotPlanV1) -> None:
    try:
        repo = Repo(str(repository))
        commit = cast(Commit, repo[repo.head()])
        if (
            repo.head().decode("ascii") != plan.prepared_commit
            or commit.tree.decode("ascii") != plan.prepared_tree
        ):
            raise ValueError("workspace identity mismatch")
    except (OSError, KeyError, UnicodeError, ValueError) as exc:
        raise QualifiedCampaignStateError("campaign workspace is invalid") from exc


def _verify_trusted_control_directory(repository: Path) -> None:
    try:
        control = repository / ".git"
        if _read_regular(control / "config", max_bytes=64 * 1024) != _TRUSTED_CONFIG:
            raise ValueError("trusted configuration changed")
        for path in (
            control / "config.worktree",
            control / "objects" / "info" / "alternates",
            control / "objects" / "info" / "http-alternates",
        ):
            if path.exists() or path.is_symlink():
                raise ValueError("untrusted Git authority appeared")
    except (OSError, ValueError) as exc:
        raise QualifiedCampaignStateError(
            "campaign Git control authority is invalid"
        ) from exc


def _required_snapshot_root() -> Path:
    return _PRODUCTION_SNAPSHOT_ROOT


def _workspace_signing_key(secret: bytes) -> Ed25519PrivateKey:
    seed = hashlib.sha256(
        b"research-supervisor-workspace-ed25519-v1\0" + secret
    ).digest()
    return Ed25519PrivateKey.from_private_bytes(seed)


def _signed_workspace_binding(
    secret: bytes,
    *,
    workspace: Path,
    campaign_public_id: str,
    launch_intent_id: str,
    launch_intent_sha256: str,
    bundle_sha256: str,
    snapshot_id: str,
    baseline_commit: str,
    baseline_tree: str,
) -> bytes:
    if len(secret) != 32:
        raise QualifiedCampaignStateError("Core workspace signing key is invalid")
    payload = {
        "authority_domain": _WORKSPACE_BINDING_DOMAIN,
        "schema_version": 1,
        "workspace_path": str(workspace.resolve(strict=False)),
        "campaign_public_id": campaign_public_id,
        "launch_intent_id": launch_intent_id,
        "launch_intent_sha256": launch_intent_sha256,
        "bundle_sha256": bundle_sha256,
        "snapshot_id": snapshot_id,
        "baseline_commit": baseline_commit,
        "baseline_tree": baseline_tree,
    }
    signature = _workspace_signing_key(secret).sign(canonical_json(payload))
    return canonical_json(
        {
            **payload,
            "signature_base64": base64.b64encode(signature).decode("ascii"),
        }
    )


def _verify_core_owned_workspace_capability(
    repository: Path, root: Path, expected_uid: int
) -> None:
    campaign = repository.parent
    binding = campaign / "snapshot-binding-v1.json"
    control = repository / ".git"
    for path, kind, forbidden_mode in (
        (root, "directory", 0o027),
        (root / "workspaces", "directory", 0o027),
        (binding, "file", 0o022),
        (control, "directory", 0o022),
    ):
        status = path.lstat()
        if (
            stat.S_ISLNK(status.st_mode)
            or status.st_uid != expected_uid
            or status.st_mode & forbidden_mode
            or (kind == "file" and not stat.S_ISREG(status.st_mode))
            or (kind == "directory" and not stat.S_ISDIR(status.st_mode))
        ):
            raise OSError("workspace capability is not Core-owned")
    campaign_status = campaign.lstat()
    if (
        stat.S_ISLNK(campaign_status.st_mode)
        or not stat.S_ISDIR(campaign_status.st_mode)
        or campaign_status.st_uid != expected_uid
        or campaign_status.st_gid != root.lstat().st_gid
        or stat.S_IMODE(campaign_status.st_mode) != 0o3770
    ):
        raise OSError("qualified campaign root is not safely delegated")
    repository_status = repository.lstat()
    if (
        stat.S_ISLNK(repository_status.st_mode)
        or not stat.S_ISDIR(repository_status.st_mode)
        or not repository_status.st_mode & stat.S_ISVTX
    ):
        raise OSError("workspace cannot protect its Core-owned control directory")
    for parent, directories, files in os.walk(control):
        for name in (*directories, *files):
            path = Path(parent) / name
            status = path.lstat()
            if (
                stat.S_ISLNK(status.st_mode)
                or status.st_uid != expected_uid
                or status.st_mode & 0o022
                or (
                    name in directories
                    and not stat.S_ISDIR(status.st_mode)
                )
                or (name in files and not stat.S_ISREG(status.st_mode))
            ):
                raise OSError("Git control authority is not Core-owned")


def _verify_production_trust_anchor(root: Path, expected_core_uid: int) -> None:
    if root != _PRODUCTION_SNAPSHOT_ROOT:
        raise OSError("production snapshot trust anchor changed")
    service_root = root.parent
    service_status = service_root.lstat()
    if (
        stat.S_ISLNK(service_status.st_mode)
        or not stat.S_ISDIR(service_status.st_mode)
        or service_status.st_uid != expected_core_uid
        or service_status.st_mode & 0o022
    ):
        raise OSError("production service storage root is unsafe")
    for parent in service_root.parents:
        status = parent.lstat()
        if (
            stat.S_ISLNK(status.st_mode)
            or not stat.S_ISDIR(status.st_mode)
            or status.st_uid != 0
            or status.st_mode & 0o022
        ):
            raise OSError("production snapshot trust-anchor ancestor is unsafe")


def _verify_worktree_matches_tree(
    workspace: Path, repo: Repo, tree_id: ObjectID
) -> None:
    expected: dict[str, tuple[int, ObjectID]] = {}
    for entry in iter_tree_contents(repo.object_store, tree_id):
        if entry.path is None or entry.mode is None or entry.sha is None:
            raise ValueError("snapshot tree entry is incomplete")
        expected[entry.path.decode("utf-8")] = (entry.mode, entry.sha)
    observed: set[str] = set()
    for path in workspace.rglob("*"):
        relative = path.relative_to(workspace)
        if relative.parts[:1] == (".git",):
            continue
        status = path.lstat()
        if stat.S_ISLNK(status.st_mode):
            raise ValueError("snapshot worktree contains a symlink")
        if stat.S_ISDIR(status.st_mode):
            continue
        name = relative.as_posix()
        planned = expected.get(name)
        if planned is None:
            raise ValueError("snapshot worktree contains an extra file")
        mode, object_id = planned
        content = _read_regular(path, max_bytes=max(status.st_size, 1))
        if Blob.from_string(content).id != object_id:
            raise ValueError("snapshot worktree content differs from its tree")
        if bool(mode & 0o111) != bool(status.st_mode & 0o111):
            raise ValueError("snapshot worktree mode differs from its tree")
        observed.add(name)
    if observed != set(expected):
        raise ValueError("snapshot worktree is incomplete")


def _snapshot_storage_root(path: Path) -> Path:
    root = _private_root(path, mode=0o710)
    for name, mode in (("imports", 0o700), ("staging", 0o700), ("complete", 0o700)):
        child = root / name
        child.mkdir(exist_ok=True, mode=mode)
        _validate_directory(child)
        os.chmod(child, mode)
    workspaces = root / "workspaces"
    production_root = _PRODUCTION_SNAPSHOT_ROOT.resolve(strict=False)
    if not workspaces.exists():
        if root == production_root:
            raise QualifiedCampaignStateError(
                "production workspace root requires installer provisioning"
            )
        workspaces.mkdir(mode=0o710)
        os.chmod(workspaces, 0o2710)
    _validate_directory(workspaces)
    if stat.S_IMODE(workspaces.lstat().st_mode) != 0o2710:
        if root == production_root:
            raise QualifiedCampaignStateError(
                "production workspace root lost installer-provisioned setgid authority"
            )
        os.chmod(workspaces, 0o2710)
    fsync_directory(root)
    return root


def initialize_snapshot_store(path: Path) -> Path:
    """Initialize all snapshot states before service IPC is accepted."""
    return _snapshot_storage_root(path)


def publish_workspace_verification_key(snapshot_root: Path, secret: bytes) -> None:
    """Durably publish the public half of the Core-only binding key."""
    if len(secret) != 32:
        raise QualifiedCampaignStateError("Core workspace signing key is invalid")
    root = _snapshot_storage_root(snapshot_root)
    expected = (
        _workspace_signing_key(secret)
        .public_key()
        .public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    )
    path = root / _WORKSPACE_PUBLIC_KEY
    if path.exists() or path.is_symlink():
        if _read_regular(path, max_bytes=32) != expected:
            raise QualifiedCampaignStateError("workspace verification key was substituted")
    else:
        _write_new_file(path, expected, mode=0o444)
        fsync_directory(root)
    status = path.lstat()
    if (
        not stat.S_ISREG(status.st_mode)
        or stat.S_ISLNK(status.st_mode)
        or stat.S_IMODE(status.st_mode) != 0o444
        or status.st_uid != root.lstat().st_uid
    ):
        raise QualifiedCampaignStateError("workspace verification key is unsafe")


def _private_root(path: Path, *, mode: int = 0o700) -> Path:
    try:
        path.mkdir(parents=True, exist_ok=True, mode=mode)
        absolute = Path(os.path.abspath(path))
        resolved = path.resolve(strict=True)
        if absolute != resolved:
            raise OSError("storage path resolves elsewhere")
        _validate_directory(resolved)
        os.chmod(resolved, mode)
        return resolved
    except (OSError, RuntimeError, ValueError) as exc:
        raise QualifiedCampaignStateError("repository storage is unavailable") from exc


def _validate_directory(path: Path) -> None:
    status = path.lstat()
    if stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode):
        raise OSError("unsafe directory")


def _trusted_bare_config() -> bytes:
    return b"[core]\n\trepositoryformatversion = 0\n\tbare = true\n"


def _replace_file(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o400)
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _write_new_file(path: Path, content: bytes, *, mode: int) -> None:
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
            raise OSError("short write")
        offset += written


def _read_regular(path: Path, *, max_bytes: int) -> bytes:
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode) or status.st_size > max_bytes:
            raise OSError("unsafe regular file")
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(_COPY_CHUNK, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        value = b"".join(chunks)
        if len(value) > max_bytes:
            raise OSError("file exceeds limit")
        return value
    finally:
        os.close(descriptor)


def _manifest_sha256(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        content = _read_regular(path, max_bytes=max(path.stat().st_size, 1))
        digest.update(relative + b"\0" + hashlib.sha256(content).digest())
    return digest.hexdigest()


def _fsync_tree(root: Path) -> None:
    directories: list[Path] = []
    for parent, names, files in os.walk(root):
        directory = Path(parent)
        directories.append(directory)
        for name in (*names, *files):
            path = directory / name
            status = path.lstat()
            if stat.S_ISLNK(status.st_mode):
                raise OSError("trusted storage contains a symlink")
            if stat.S_ISREG(status.st_mode):
                descriptor = os.open(
                    path,
                    os.O_RDONLY
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                )
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
    for directory in reversed(directories):
        fsync_directory(directory)


def _make_tree_read_only(root: Path) -> None:
    for parent, directories, files in os.walk(root):
        for name in files:
            path = Path(parent) / name
            executable = bool(path.stat(follow_symlinks=False).st_mode & 0o111)
            os.chmod(path, 0o500 if executable else 0o400, follow_symlinks=False)
        for name in directories:
            os.chmod(Path(parent) / name, 0o500, follow_symlinks=False)
    os.chmod(root, 0o500)


def _copy_mutable_workspace(source: Path, destination: Path) -> None:
    """Copy a workspace without any runtime request to set SUID/SGID bits.

    The root installer owns the sole privileged step: provisioning ``workspaces``
    as a Core-owned ``02710`` directory.  Linux then propagates that directory's
    group and SGID bit to mutable descendants.  A narrow temporary umask preserves
    their intended group permissions at creation time, so the service remains
    compatible with ``RestrictSUIDSGID=true``.  Core's IPC server is synchronous;
    no other service thread can create authority material during this scope.
    """
    previous_umask = os.umask(0o007)
    try:
        destination.mkdir(mode=0o1770)
        if stat.S_IMODE(destination.lstat().st_mode) != 0o3770:
            raise QualifiedCampaignStateError(
                "mutable workspace did not inherit setgid authority"
            )
        for parent, directories, files in os.walk(source):
            directories.sort()
            files.sort()
            source_parent = Path(parent)
            for name in directories:
                source_path = source_parent / name
                source_status = source_path.lstat()
                if stat.S_ISLNK(source_status.st_mode) or not stat.S_ISDIR(
                    source_status.st_mode
                ):
                    raise QualifiedCampaignStateError(
                        "sanitized snapshot contains an unsafe workspace directory"
                    )
                relative = source_path.relative_to(source)
                protected = relative.parts[:1] == (".git",)
                target = destination / relative
                target.mkdir(mode=0o700 if protected else 0o770)
                if protected:
                    # Clear inherited SGID before creating control descendants.
                    # Clearing a privilege bit is permitted by RestrictSUIDSGID.
                    os.chmod(target, 0o700, follow_symlinks=False)
                elif stat.S_IMODE(target.lstat().st_mode) != 0o2770:
                    raise QualifiedCampaignStateError(
                        "mutable workspace directory did not inherit setgid authority"
                    )
            for name in files:
                source_path = source_parent / name
                source_status = source_path.lstat()
                if stat.S_ISLNK(source_status.st_mode) or not stat.S_ISREG(
                    source_status.st_mode
                ):
                    raise QualifiedCampaignStateError(
                        "sanitized snapshot contains an unsafe workspace file"
                    )
                relative = source_path.relative_to(source)
                protected = relative.parts[:1] == (".git",)
                executable = bool(source_status.st_mode & 0o111)
                mode = 0o600 if protected else (0o770 if executable else 0o660)
                content = _read_regular(
                    source_path, max_bytes=max(source_status.st_size, 1)
                )
                _write_new_file(destination / relative, content, mode=mode)
        _seal_workspace_control(destination / ".git")
    finally:
        os.umask(previous_umask)


def _seal_workspace_control(control: Path) -> None:
    """Make copied Git authority Core-owned and immutable to the shared group."""
    for parent, directories, files in os.walk(control, topdown=False):
        for name in files:
            os.chmod(Path(parent) / name, 0o440, follow_symlinks=False)
        for name in directories:
            os.chmod(Path(parent) / name, 0o550, follow_symlinks=False)
    os.chmod(control, 0o550, follow_symlinks=False)


def _remove_workspace_staging(path: Path, workspaces: Path) -> None:
    """Remove only an unpublished staging directory beneath ``workspaces``."""
    if (
        path.is_symlink()
        or path.parent.resolve(strict=True) != workspaces.resolve(strict=True)
        or not path.name.startswith(".campaign-")
    ):
        raise QualifiedCampaignStateError("refused unsafe workspace staging cleanup")
    for parent, directories, _files in os.walk(path):
        os.chmod(parent, 0o700, follow_symlinks=False)
        for name in directories:
            os.chmod(Path(parent) / name, 0o700, follow_symlinks=False)
    shutil.rmtree(path)


def orphan_staging_directories(snapshot_root: Path) -> Iterator[Path]:
    """Enumerate only non-authoritative building artifacts for qualified GC."""
    staging = _snapshot_storage_root(snapshot_root) / "staging"
    for path in sorted(staging.iterdir()):
        if path.is_dir() and not path.is_symlink():
            yield path
