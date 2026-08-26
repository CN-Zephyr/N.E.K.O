"""Frozen dataclass types for the plugin install source lock.

All timestamps are carried as already-normalized strings in the format
``%Y-%m-%dT%H:%M:%S.%fZ`` (UTC). Normalization happens in the Parser; the
models themselves do not validate or coerce timestamps.

``frozen=True`` lets writers publish new :class:`LockFile` snapshots via
a single attribute assignment on the manager — readers always observe a
fully consistent :class:`LockFile` whether they take the pre- or
post-publish state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal, Mapping


RootId = Literal["builtin", "user"]
Channel = Literal["builtin", "manual", "imported", "market"]
Reason = Literal["user_requested", "auto_dependency"]


@dataclass(frozen=True)
class SourceDetailMarket:
    """``source_detail`` for ``channel="market"`` entries.

    v2 schema (design §3.1.1): the lock entry carries the same evidence
    Market is already distributing — channel, sha256, payload hash, and
    publish timestamp — so a lock file alone is enough to identify which
    package is on disk and on which release channel it was installed.

    Field order intentionally matches the on-disk serialization order
    defined in design §3.1.3 (``plugin_market_id`` → ``version`` →
    ``channel`` → ``package_url`` → ``package_sha256`` → ``payload_hash``
    → ``published_at`` → ``previous_version``). The serializer writes
    fields in this declaration order via Python 3.7+ dict insertion
    ordering, so reordering this dataclass changes the on-disk byte
    layout — keep it stable.
    """

    plugin_market_id: str
    version: str
    package_url: str
    # v2 (R2.1, R2.9): Market-distributed evidence baked into the lock.
    # ``package_sha256``: 64-char lowercase hex; ``""`` when unknown
    # (legacy v1 row promoted via _parse_source_detail).
    package_sha256: str
    # ``payload_hash``: SHA-256 of unpacked metadata.toml [payload].hash;
    # may be None when the package omits it.
    payload_hash: str | None
    # ``channel``: "stable" | "beta"; default "stable" when missing.
    channel: str
    # ``published_at``: Market-side ``latest_version.created_at``;
    # falls back to ``LockEntry.installed_at`` on legacy rows.
    published_at: str
    # Captured on upgrade (old version before the current write); None on
    # first install and on no-op same-version re-calls.
    previous_version: str | None = None


@dataclass(frozen=True)
class SourceDetailImported:
    """``source_detail`` for ``channel="imported"`` entries."""

    package_filename: str
    package_sha256: str  # 64-char lowercase hex


# builtin / manual channels carry source_detail=None.
SourceDetail = SourceDetailMarket | SourceDetailImported | None


@dataclass(frozen=True)
class LockEntry:
    """One plugin's install-source record.

    Primary key is ``(root_id, directory_name)``. ``plugin_id`` may be ``""``
    when the directory's metadata was temporarily unreadable.
    """

    root_id: RootId
    directory_name: str
    plugin_id: str
    channel: Channel
    reason: Reason
    installed_at: str
    updated_at: str
    last_seen_at: str
    removed: bool = False
    removed_at: str | None = None
    source_detail: SourceDetail = None
    # Package-profile directory key. Empty on legacy rows that predate
    # package identity tracking; callers may then fall back to plugin_id.
    package_id: str = ""
    # Absolute profile location selected at install time. Empty on legacy
    # rows, which use the default profile root and package_id fallback.
    profile_dir: str = ""
    # ``None`` denotes a legacy row written before profile ownership was
    # tracked. New package installs write ``True`` only when they unpacked a
    # profile, so deletion never mistakes a later user-created directory for
    # package-owned configuration.
    profile_installed: bool | None = None

    @property
    def primary_key(self) -> tuple[str, str]:
        return (self.root_id, self.directory_name)


@dataclass(frozen=True)
class LockFile:
    """Top-level lock file snapshot."""

    schema_version: int
    entries: tuple[LockEntry, ...]
    updated_at: str
    # Written only on First_Startup migration; preserved thereafter.
    created_at: str | None = None


# ---------------------------------------------------------------------------
# Schema v3: unified inventory (candidates + selection + state ownership)
# ---------------------------------------------------------------------------
# v3 keeps every v1/v2 provenance field but regroups the flat ``entries``
# list under its logical PluginId, and folds in the selection and
# state-ownership receipts that v2 kept in a second file
# (``plugin_candidate_selections.json``). Nothing here is a new product
# concept: it is the same data with one owner and one revision.


INVENTORY_SCHEMA_VERSION = 3


@dataclass(frozen=True)
class CandidateRecord:
    """One installed candidate's provenance, keyed inside a PluginEntry.

    Field-for-field equivalent to :class:`LockEntry` minus ``plugin_id``,
    which becomes the enclosing :class:`PluginEntry` key. Keeping the field
    names identical lets the v2 → v3 conversion stay mechanical and lets the
    serializer reuse :func:`_serialize_source_detail_for_json`.
    """

    root_id: RootId
    directory_name: str
    channel: Channel
    reason: Reason
    installed_at: str
    updated_at: str
    last_seen_at: str
    removed: bool = False
    removed_at: str | None = None
    source_detail: SourceDetail = None
    package_id: str = ""
    profile_dir: str = ""
    profile_installed: bool | None = None

    @property
    def primary_key(self) -> tuple[str, str]:
        return (self.root_id, self.directory_name)

    @classmethod
    def from_lock_entry(cls, entry: LockEntry) -> "CandidateRecord":
        """Project a v2 :class:`LockEntry` into a v3 candidate record."""

        return cls(
            root_id=entry.root_id,
            directory_name=entry.directory_name,
            channel=entry.channel,
            reason=entry.reason,
            installed_at=entry.installed_at,
            updated_at=entry.updated_at,
            last_seen_at=entry.last_seen_at,
            removed=entry.removed,
            removed_at=entry.removed_at,
            source_detail=entry.source_detail,
            package_id=entry.package_id,
            profile_dir=entry.profile_dir,
            profile_installed=entry.profile_installed,
        )

    def to_lock_entry(self, plugin_id: str) -> LockEntry:
        """Re-attach ``plugin_id`` so v2 readers keep working during cutover."""

        return LockEntry(
            root_id=self.root_id,
            directory_name=self.directory_name,
            plugin_id=plugin_id,
            channel=self.channel,
            reason=self.reason,
            installed_at=self.installed_at,
            updated_at=self.updated_at,
            last_seen_at=self.last_seen_at,
            removed=self.removed,
            removed_at=self.removed_at,
            source_detail=self.source_detail,
            package_id=self.package_id,
            profile_dir=self.profile_dir,
            profile_installed=self.profile_installed,
        )


@dataclass(frozen=True)
class CandidateRef:
    """Reference to one candidate slot inside the same PluginEntry.

    Structurally identical to the domain ``CandidateKey``; kept local so this
    module stays import-free and the on-disk shape has exactly one owner.
    """

    root_id: RootId
    directory_name: str

    @property
    def primary_key(self) -> tuple[str, str]:
        return (self.root_id, self.directory_name)


@dataclass(frozen=True)
class StateOwnership:
    """Who is authorised to read the logical id's existing shared state.

    Mirrors the receipt v2 stored under ``state_owners`` in
    ``plugin_candidate_selections.json``. ``state_scope`` is currently always
    ``"legacy_shared"`` or ``None`` — v3 does not introduce a new scope.
    """

    candidate: CandidateRef
    state_scope: str | None = None
    state_access_grant: str | None = None
    release_chain_id: str | None = None
    authorized_at: str | None = None


@dataclass(frozen=True)
class PluginEntry:
    """Everything durable about one logical PluginId.

    Invariants the codec and writers must preserve:

    * ``selected_candidate``, when set, names a live (``removed=False``)
      member of ``candidates``;
    * ``state_owner`` may name a ``removed=True`` candidate — losing the code
      must not silently drop the data-ownership receipt;
    * ``enabled`` is desired runtime intent only; actual process state never
      lands here.
    """

    plugin_id: str
    candidates: tuple[CandidateRecord, ...] = ()
    selected_candidate: CandidateRef | None = None
    candidate_source: Channel | None = None
    enabled: bool = True
    state_owner: StateOwnership | None = None

    def candidate_for(self, ref: CandidateRef) -> CandidateRecord | None:
        return next(
            (c for c in self.candidates if c.primary_key == ref.primary_key),
            None,
        )

    def live_candidates(self) -> tuple[CandidateRecord, ...]:
        return tuple(c for c in self.candidates if not c.removed)


@dataclass(frozen=True)
class PluginInventory:
    """Top-level v3 snapshot: the single durable desired-state authority.

    Replaces the ``plugins.lock.json`` + ``plugin_candidate_selections.json``
    pair. ``revision`` is a monotonic counter used for compare-and-set writes
    so two processes touching different PluginIds cannot lose each other's
    changes.

    This is a JSON document under the plugin config root. It is unrelated to
    the Windows registry.
    """

    schema_version: int = INVENTORY_SCHEMA_VERSION
    plugins: Mapping[str, PluginEntry] = field(
        default_factory=lambda: MappingProxyType({})
    )
    revision: int = 1
    updated_at: str = ""
    created_at: str | None = None

    @classmethod
    def build(
        cls,
        plugins: Mapping[str, PluginEntry],
        *,
        revision: int = 1,
        updated_at: str,
        created_at: str | None = None,
        schema_version: int = INVENTORY_SCHEMA_VERSION,
    ) -> "PluginInventory":
        """Freeze ``plugins`` so readers can share a snapshot without copying."""

        return cls(
            schema_version=schema_version,
            plugins=MappingProxyType(dict(plugins)),
            revision=revision,
            updated_at=updated_at,
            created_at=created_at,
        )

    def entry(self, plugin_id: str) -> PluginEntry | None:
        return self.plugins.get(plugin_id)

    def with_entry(self, entry: PluginEntry) -> "PluginInventory":
        """Return a snapshot with one PluginEntry replaced.

        ``revision`` and ``updated_at`` are deliberately untouched: bumping
        them is the writer's job so a single durable write can carry several
        entry mutations.
        """

        merged = dict(self.plugins)
        merged[entry.plugin_id] = entry
        return PluginInventory(
            schema_version=self.schema_version,
            plugins=MappingProxyType(merged),
            revision=self.revision,
            updated_at=self.updated_at,
            created_at=self.created_at,
        )
