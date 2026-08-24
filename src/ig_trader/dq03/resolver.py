"""Deterministic, cache-aware, read-only IG Demo contract resolver."""

# ruff: noqa: E501

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Protocol

from src.ig_trader.dq03.models import (
    CandidateEvidence,
    DataStatus,
    DQ03Resolution,
    DQ03Status,
    MarketMetadata,
    RequestCounters,
    metadata_from_transport,
)
from src.ig_trader.dq03.registry import InstrumentSearchRule, search_rule
from src.ig_trader.strategy_lab.models import INITIAL_INSTRUMENTS, AssetClass, InstrumentSpec


class DQ03Transport(Protocol):
    """The read-only subset of IG Demo transport used by the resolver."""

    def search_markets(self, search_term: str) -> tuple[dict[str, object], ...]: ...

    def get_market(self, epic: str) -> object: ...

    def get_markets(self, epics: tuple[str, ...]) -> Mapping[str, object]: ...


_PRODUCT_REJECT_TERMS = (
    "weekend",
    "etf",
    "etc",
    "etn",
    "leveraged",
    "share",
    "equity",
    "stock",
    "plc",
    "ltd",
    "limited",
    "inc",
    "corp",
    "corporation",
    "group",
    "mining",
    "gaming",
    "entertainment",
    "fund",
    "company",
    "expired",
    "future",
    "futures",
)
_EXPECTED_TYPES = {
    AssetClass.FX: ("currenc", "forex", "fx"),
    # IG's physical precious-metal contracts can be labelled CURRENCIES; the
    # canonical metal identity and all cash/spot metadata checks still apply.
    AssetClass.METAL: ("commod", "metal", "currenc"),
    AssetClass.INDEX: ("indice", "index"),
    AssetClass.ENERGY: ("commod", "energy"),
}


class DQ03InstrumentResolver:
    """Resolve one trusted contract only when the evidence separates it safely.

    Searches and metadata calls are cached for the lifetime of one resolver.
    The class makes only ``GET``-style transport calls; the supplied transport's
    mutation methods are never referenced.
    """

    def __init__(
        self,
        transport: DQ03Transport,
        *,
        shortlist_limit: int = 3,
        selection_margin: int = 2,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        counters: RequestCounters | None = None,
    ) -> None:
        if shortlist_limit < 1 or selection_margin < 0:
            raise ValueError("DQ-03 selection limits must be positive")
        self._transport = transport
        self._shortlist_limit = shortlist_limit
        self._selection_margin = selection_margin
        self._clock = clock
        self.counters = counters or RequestCounters()
        self._search_cache: dict[str, tuple[dict[str, object], ...]] = {}
        self._metadata_cache: dict[str, MarketMetadata] = {}
        self._metadata_failures: dict[str, str] = {}

    def resolve_universe(
        self, instruments: tuple[InstrumentSpec, ...] = INITIAL_INSTRUMENTS
    ) -> tuple[DQ03Resolution, ...]:
        """Return all requested symbols even if a safe selection cannot be made."""

        specs = tuple(_instrument_spec(instrument) for instrument in instruments)
        observed: dict[str, tuple[_ObservedCandidate, ...]] = {}
        failed: dict[str, str] = {}
        for spec in specs:
            try:
                observed[spec.symbol] = self._collect_candidates(spec, search_rule(spec))
            except RuntimeError as error:
                failed[spec.symbol] = _safe_error(error)
                observed[spec.symbol] = ()

        # Batch the bounded candidate shortlists across the whole universe.  This
        # is deliberately before resolution: one V2 request can verify contracts
        # discovered from different symbols, rather than one V4 request each.
        candidates_to_fetch: list[str] = []
        for spec in specs:
            rule = search_rule(spec)
            viable = [
                item
                for item in observed[spec.symbol]
                if not _hard_rejection(spec, rule, item) and item.epic
            ]
            viable.sort(
                key=lambda item: (_pre_score(spec, rule, item), item.epic or ""), reverse=True
            )
            candidates_to_fetch.extend(
                item.epic for item in viable[: self._shortlist_limit] if item.epic
            )
        try:
            self._prefetch_metadata(tuple(dict.fromkeys(candidates_to_fetch)))
        except RuntimeError as error:
            if not getattr(error, "classification", None):
                raise
            # Every result remains explicit below; no retry is attempted after an
            # allowance/authentication error.
            for spec in specs:
                failed.setdefault(spec.symbol, _safe_error(error))

        results: list[DQ03Resolution] = []
        for spec in specs:
            if spec.symbol in failed:
                results.append(
                    DQ03Resolution(
                        spec.symbol,
                        spec.asset_class,
                        DQ03Status.METADATA_INCOMPLETE,
                        None,
                        spec.display_name,
                        None,
                        0,
                        None,
                        ("Read-only discovery could not be completed safely.",),
                        (),
                        None,
                        DataStatus.DATA_NOT_AVAILABLE,
                        self._clock().astimezone(UTC),
                        error=failed[spec.symbol],
                    )
                )
            else:
                results.append(self._resolve_observed(spec, observed[spec.symbol]))
        return tuple(results)

    def resolve_symbol(self, instrument: InstrumentSpec | str) -> DQ03Resolution:
        """Resolve exactly one canonical symbol without defaulting missing broker facts."""

        return self.resolve_universe((_instrument_spec(instrument),))[0]

    def _resolve_observed(
        self, spec: InstrumentSpec, observed: tuple[_ObservedCandidate, ...]
    ) -> DQ03Resolution:
        rule = search_rule(spec)
        observed_at = self._clock().astimezone(UTC)
        if not observed:
            return _resolution(
                spec,
                DQ03Status.NOT_FOUND,
                (),
                observed_at,
                reasons=("No IG Demo search candidate was returned for the declared aliases.",),
            )

        candidates, eligible = self._evaluate_candidates(spec, rule, observed)
        if not eligible:
            return _resolution(
                spec,
                _blocked_status(candidates),
                tuple(candidates),
                observed_at,
                reasons=_blocked_reasons(candidates),
            )
        ranked = sorted(eligible, key=lambda item: (-item.score, item.epic))
        winner = ranked[0]
        if len(ranked) > 1 and winner.score - ranked[1].score <= self._selection_margin:
            return _resolution(
                spec,
                DQ03Status.AMBIGUOUS,
                tuple(candidates),
                observed_at,
                reasons=(
                    "More than one complete, tradeable cash/spot candidate remains within the "
                    "reviewed selection margin.",
                ),
            )
        selected = tuple(replace(item, selected=item.epic == winner.epic) for item in candidates)
        return DQ03Resolution(
            symbol=spec.symbol,
            asset_class=spec.asset_class,
            classification=DQ03Status.VERIFIED,
            selected_epic=winner.epic,
            display_name=winner.metadata.display_name or spec.display_name,
            selected_alias=winner.aliases[0] if winner.aliases else None,
            candidate_count=len(observed),
            selection_score=winner.score,
            selection_reasons=winner.reasons,
            candidates=selected,
            metadata=winner.metadata,
            data_status=DataStatus.BROKER_VALIDATION_PENDING,
            observed_at=observed_at,
        )

    def _collect_candidates(
        self, spec: InstrumentSpec, rule: InstrumentSearchRule
    ) -> tuple[_ObservedCandidate, ...]:
        by_epic: dict[str, _ObservedCandidate] = {}
        invalid: list[_ObservedCandidate] = []
        for alias in rule.aliases:
            search_candidates = self._search(alias)
            for candidate in search_candidates:
                epic = _text(candidate.get("epic"))
                value = _ObservedCandidate.from_document(candidate, alias)
                if epic is None:
                    invalid.append(value)
                    continue
                previous = by_epic.get(epic)
                by_epic[epic] = value if previous is None else previous.with_alias(alias)
            # A useful canonical search already returned a compatible contract;
            # aliases are fallbacks, not permission to repeat equivalent searches.
            if any(not _hard_rejection(spec, rule, value) for value in by_epic.values()):
                break
        return tuple(by_epic.values()) + tuple(invalid)

    def _search(self, alias: str) -> tuple[dict[str, object], ...]:
        cached = self._search_cache.get(alias)
        if cached is not None:
            return cached
        result = self._transport.search_markets(alias)
        if not isinstance(result, tuple) or not all(isinstance(item, Mapping) for item in result):
            raise RuntimeError("IG market search response has an invalid candidate shape")
        self.counters.search_request_count += 1
        self._search_cache[alias] = result
        return result

    def _metadata(self, epic: str) -> MarketMetadata:
        cached = self._metadata_cache.get(epic)
        if cached is not None:
            return cached
        failure = self._metadata_failures.get(epic)
        if failure:
            raise RuntimeError(failure)
        try:
            raw = self._transport.get_market(epic)
        except RuntimeError as error:
            self._metadata_failures[epic] = _safe_error(error)
            raise
        self.counters.single_metadata_request_count += 1
        try:
            metadata = metadata_from_transport(raw)
        except (AttributeError, TypeError, ValueError) as error:
            raise RuntimeError("IG market metadata response has an invalid shape") from error
        if metadata.epic != epic:
            raise RuntimeError("IG market metadata EPIC does not match the requested candidate")
        self._metadata_cache[epic] = metadata
        return metadata

    def _prefetch_metadata(self, epics: tuple[str, ...]) -> None:
        """Fill the cache through V2 batches, with V4 only for omitted results."""

        missing = [epic for epic in epics if epic not in self._metadata_cache]
        get_markets = getattr(self._transport, "get_markets", None)
        if callable(get_markets):
            for start in range(0, len(missing), 10):
                batch = tuple(missing[start : start + 10])
                if not batch:
                    continue
                try:
                    raw = get_markets(batch)
                except RuntimeError as error:
                    # An allowance/authentication 403 is a hard safe-stop; it
                    # must never fan out into further V4 calls.  A non-403 V2
                    # server failure means that batch did not provide metadata,
                    # so one bounded V4 read per affected EPIC is the explicit
                    # compatibility fallback, not a retry of the failed batch.
                    if getattr(error, "classification", None):
                        raise
                    for epic in batch:
                        self._metadata_or_record_failure(epic)
                    continue
                if not isinstance(raw, Mapping):
                    for epic in batch:
                        self._metadata_or_record_failure(epic)
                    continue
                self.counters.batched_metadata_request_count += 1
                for epic, value in raw.items():
                    if epic not in batch:
                        continue
                    metadata = metadata_from_transport(value)
                    if metadata.epic != epic:
                        raise RuntimeError(
                            "IG batched market metadata EPIC does not match candidate"
                        )
                    self._metadata_cache[epic] = metadata
        # IG V2 may omit an EPIC; one V4 confirmation is then an explicit
        # fallback, never a blind retry of a failed 403 response.
        for epic in epics:
            if epic not in self._metadata_cache and epic not in self._metadata_failures:
                self._metadata_or_record_failure(epic)

    def _metadata_or_record_failure(self, epic: str) -> None:
        try:
            self._metadata(epic)
        except RuntimeError as error:
            if getattr(error, "classification", None):
                raise
            self._metadata_failures[epic] = _safe_error(error)

    def _evaluate_candidates(
        self,
        spec: InstrumentSpec,
        rule: InstrumentSearchRule,
        observed: tuple[_ObservedCandidate, ...],
    ) -> tuple[list[CandidateEvidence], list[_ScoredCandidate]]:
        preliminary: list[CandidateEvidence] = []
        pending: list[_ObservedCandidate] = []
        for item in observed:
            rejected = _hard_rejection(spec, rule, item)
            if rejected:
                preliminary.append(item.evidence(reasons=rejected))
            else:
                pending.append(item)
        pending.sort(key=lambda item: (_pre_score(spec, rule, item), item.epic or ""), reverse=True)
        overflow = pending[self._shortlist_limit :]
        preliminary.extend(
            item.evidence(
                reasons=(
                    "Candidate was outside the bounded metadata shortlist; no safe selection was "
                    "made from it.",
                )
            )
            for item in overflow
        )
        scored: list[_ScoredCandidate] = []
        for item in pending[: self._shortlist_limit]:
            if item.epic is None:
                continue
            try:
                metadata = self._metadata(item.epic)
            except RuntimeError as error:
                preliminary.append(
                    item.evidence(
                        score=_pre_score(spec, rule, item),
                        reasons=(f"Metadata unavailable: {_safe_error(error)}",),
                    )
                )
                continue
            reasons = _metadata_rejection(spec, rule, item, metadata)
            if reasons:
                preliminary.append(
                    item.evidence(
                        metadata=metadata,
                        score=_pre_score(spec, rule, item),
                        reasons=reasons,
                    )
                )
                continue
            score, score_reasons = _score_candidate(spec, item, metadata)
            scored.append(_ScoredCandidate(item, metadata, score, score_reasons))
        _apply_contract_size_preference(scored)
        approved = {item.epic: item for item in scored}
        evaluated = [
            _evidence_from_scored(item) if item.epic in approved else item for item in preliminary
        ]
        evaluated.extend(_evidence_from_scored(item) for item in scored)
        return sorted(
            evaluated, key=lambda item: (item.epic or "", item.display_name or "")
        ), scored


@dataclass(frozen=True)
class _ObservedCandidate:
    epic: str | None
    display_name: str | None
    instrument_type: str | None
    expiry: str | None
    market_status: str | None
    aliases: tuple[str, ...]

    @classmethod
    def from_document(cls, value: Mapping[str, object], alias: str) -> _ObservedCandidate:
        return cls(
            epic=_text(value.get("epic")),
            display_name=_text(value.get("name")),
            instrument_type=_text(value.get("type")),
            expiry=_text(value.get("expiry")),
            market_status=_text(value.get("market_status")),
            aliases=(alias,),
        )

    def with_alias(self, alias: str) -> _ObservedCandidate:
        return replace(
            self, aliases=self.aliases if alias in self.aliases else (*self.aliases, alias)
        )

    def evidence(
        self,
        *,
        reasons: tuple[str, ...],
        metadata: MarketMetadata | None = None,
        score: int | None = None,
    ) -> CandidateEvidence:
        return CandidateEvidence(
            self.epic,
            self.display_name,
            self.instrument_type,
            self.expiry,
            self.market_status,
            self.aliases,
            score,
            False,
            reasons,
            metadata,
        )


@dataclass(frozen=True)
class _ScoredCandidate:
    observed: _ObservedCandidate
    metadata: MarketMetadata
    score: int
    reasons: tuple[str, ...]

    @property
    def epic(self) -> str:
        assert self.observed.epic is not None
        return self.observed.epic

    @property
    def aliases(self) -> tuple[str, ...]:
        return self.observed.aliases


def _instrument_spec(value: InstrumentSpec | str) -> InstrumentSpec:
    if isinstance(value, InstrumentSpec):
        return value
    symbol = value.upper()
    try:
        return next(item for item in INITIAL_INSTRUMENTS if item.symbol == symbol)
    except StopIteration as error:
        raise ValueError(f"unknown DQ-03 research symbol: {value!r}") from error


def _hard_rejection(
    spec: InstrumentSpec, rule: InstrumentSearchRule, candidate: _ObservedCandidate
) -> tuple[str, ...]:
    if candidate.epic is None:
        return ("Candidate has no valid IG EPIC.",)
    text = _normalized(_candidate_text(candidate))
    if "weekend" in text or ".sun" in candidate.epic.casefold():
        return ("Weekend products are excluded.",)
    if any(term in text for term in _PRODUCT_REJECT_TERMS):
        return ("Candidate is an excluded equity, ETP, expired, or futures-style product.",)
    if not _identity_matches(spec, rule, candidate):
        return ("Candidate does not match the canonical underlying identity.",)
    candidate_type = _normalized(candidate.instrument_type or "")
    if candidate_type and not any(
        term in candidate_type for term in _EXPECTED_TYPES[spec.asset_class]
    ):
        return ("Candidate type does not match the required asset class.",)
    return ()


def _metadata_rejection(
    spec: InstrumentSpec,
    rule: InstrumentSearchRule,
    candidate: _ObservedCandidate,
    metadata: MarketMetadata,
) -> tuple[str, ...]:
    metadata_candidate = replace(
        candidate,
        display_name=metadata.display_name or candidate.display_name,
        instrument_type=metadata.instrument_type or candidate.instrument_type,
        expiry=metadata.expiry or candidate.expiry,
        market_status=metadata.market_status or candidate.market_status,
    )
    rejected = _hard_rejection(spec, rule, metadata_candidate)
    if rejected:
        return rejected
    if not _cash_or_spot_target(spec, metadata):
        return ("Candidate is not a demonstrated cash/spot/rolling contract for this target.",)
    if metadata.market_status != "TRADEABLE":
        return ("Candidate is not currently tradeable according to IG market metadata.",)
    if not metadata.complete:
        return ("Required IG dealing, pricing, or streaming metadata is incomplete.",)
    return ()


def _identity_matches(
    spec: InstrumentSpec, rule: InstrumentSearchRule, candidate: _ObservedCandidate
) -> bool:
    text = _normalized(_candidate_text(candidate))
    if spec.asset_class is AssetClass.FX:
        return spec.symbol.casefold() in text
    return any(_normalized(term) in text for term in rule.identity_terms)


def _cash_or_spot_target(spec: InstrumentSpec, metadata: MarketMetadata) -> bool:
    text = _normalized(" ".join(filter(None, (metadata.display_name, metadata.expiry))))
    expiry = (metadata.expiry or "").upper()
    rolling = expiry in {"DFB", "-"}
    if spec.asset_class is AssetClass.FX:
        return rolling
    if spec.asset_class is AssetClass.METAL:
        return rolling and any(term in text for term in ("spot", "cash", "aucomptant"))
    if spec.asset_class is AssetClass.INDEX:
        return rolling and any(term in text for term in ("cash", "aucomptant", "comptant"))
    return rolling


def _pre_score(
    spec: InstrumentSpec, rule: InstrumentSearchRule, candidate: _ObservedCandidate
) -> int:
    del rule
    text = _normalized(_candidate_text(candidate))
    score = 50
    if spec.asset_class is AssetClass.FX and "mini" in text:
        score += 10
    if any(term in text for term in ("cash", "spot", "aucomptant")):
        score += 10
    if (candidate.expiry or "").upper() in {"DFB", "-"}:
        score += 8
    return score


def _score_candidate(
    spec: InstrumentSpec, candidate: _ObservedCandidate, metadata: MarketMetadata
) -> tuple[int, tuple[str, ...]]:
    score = 70
    reasons = ["Canonical underlying identity and asset class match."]
    score += 12
    reasons.append("Cash/spot or rolling contract is confirmed by IG metadata.")
    if spec.asset_class is AssetClass.FX and "mini" in _normalized(_candidate_text(candidate)):
        score += 8
        reasons.append("FX Mini contract preference applied.")
    if metadata.streaming_prices_available:
        score += 3
        reasons.append("IG reports streaming prices are available.")
    score += 5
    reasons.append("IG reports the market as TRADEABLE with complete metadata.")
    return score, tuple(reasons)


def _apply_contract_size_preference(candidates: list[_ScoredCandidate]) -> None:
    sizes = sorted(
        {item.metadata.minimum_deal_size for item in candidates if item.metadata.minimum_deal_size}
    )
    if not sizes:
        return
    replacement: list[_ScoredCandidate] = []
    for item in candidates:
        size = item.metadata.minimum_deal_size
        if size is None:
            replacement.append(item)
            continue
        rank = sizes.index(size)
        bonus = max(0, 10 - (rank * 4))
        reasons = (*item.reasons, "Smallest practical verified deal size preference applied.")
        replacement.append(replace(item, score=item.score + bonus, reasons=reasons))
    candidates[:] = replacement


def _evidence_from_scored(item: _ScoredCandidate) -> CandidateEvidence:
    return CandidateEvidence(
        item.epic,
        item.metadata.display_name or item.observed.display_name,
        item.metadata.instrument_type or item.observed.instrument_type,
        item.metadata.expiry or item.observed.expiry,
        item.metadata.market_status or item.observed.market_status,
        item.aliases,
        item.score,
        False,
        item.reasons,
        item.metadata,
    )


def _blocked_status(candidates: list[CandidateEvidence]) -> DQ03Status:
    if any("not currently tradeable" in " ".join(item.reasons).casefold() for item in candidates):
        return DQ03Status.UNTRADEABLE
    if any("metadata" in " ".join(item.reasons).casefold() for item in candidates):
        return DQ03Status.METADATA_INCOMPLETE
    return DQ03Status.UNSUPPORTED_PRODUCT


def _blocked_reasons(candidates: list[CandidateEvidence]) -> tuple[str, ...]:
    unique = tuple(dict.fromkeys(reason for item in candidates for reason in item.reasons))
    return unique or ("No candidate remained after safe product and identity checks.",)


def _resolution(
    spec: InstrumentSpec,
    status: DQ03Status,
    candidates: tuple[CandidateEvidence, ...],
    observed_at: datetime,
    *,
    reasons: tuple[str, ...],
) -> DQ03Resolution:
    retained = (
        max(
            (item for item in candidates if item.epic),
            key=lambda item: (item.metadata is not None, item.score or 0, item.epic or ""),
            default=None,
        )
        if status is DQ03Status.METADATA_INCOMPLETE
        else None
    )
    selected = tuple(
        replace(item, selected=retained is not None and item.epic == retained.epic)
        for item in candidates
    )
    return DQ03Resolution(
        spec.symbol,
        spec.asset_class,
        status,
        retained.epic if retained else None,
        (
            (retained.metadata.display_name if retained.metadata else None)
            or retained.display_name
            or spec.display_name
        )
        if retained
        else spec.display_name,
        retained.aliases[0] if retained and retained.aliases else None,
        len(candidates),
        retained.score if retained else None,
        reasons,
        selected,
        retained.metadata if retained else None,
        DataStatus.DATA_NOT_AVAILABLE,
        observed_at,
    )


def _candidate_text(candidate: _ObservedCandidate) -> str:
    return " ".join(
        value
        for value in (candidate.epic, candidate.display_name, candidate.instrument_type)
        if value
    )


def _normalized(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum())


def _text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _safe_error(error: Exception) -> str:
    return str(error).replace("\n", " ")[:180]
