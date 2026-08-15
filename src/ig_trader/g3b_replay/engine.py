"""Deterministic exact frozen Scalper replay with conservative execution semantics."""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
from math import isfinite
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import pandas as pd

from src.ig_trader.g3a_data import CanonicalCandle
from src.ig_trader.g3b_replay.account_state import G2QualificationState
from src.ig_trader.g3b_replay.data import (
    AUTHORITATIVE_GAP_EPIC,
    AUTHORITATIVE_GAP_RESOLUTION,
    AUTHORITATIVE_GAP_TIMESTAMP,
    FROZEN_REPLAY_INSTRUMENTS,
    FROZEN_RESOLUTIONS,
    ReplayDataset,
)
from src.ig_trader.models import SignalDirection
from src.ig_trader.offline_paper.conductor import FrozenV1Config, PortfolioRisk
from src.ig_trader.offline_paper.domain import (
    AccountSnapshot,
    BrokerOrder,
    ExecutionMode,
    Exit,
    LifecycleState,
    Quote,
    RiskDecision,
    Side,
    Signal,
    TradeCandidate,
    TradeIntent,
)
from src.ig_trader.offline_paper.paper_broker import PaperBroker
from src.ig_trader.strategies.scalper import ScalperStrategy

REPLAY_ENGINE_VERSION = "g3b-account-state-replay/2.0.0"
GAP_POLICY_ID = "GAP_AWARE_REPLAY_V1"
STRATEGY_VERSION = "Scalper:rsi-adx-v1"


@dataclass(frozen=True)
class ExecutionOutcome:
    price: float
    reason: str
    ambiguous_intrabar: bool


@dataclass(frozen=True)
class ReplayTrade:
    candidate_id: str
    intent_id: str
    position_reference: str
    epic: str
    side: Side
    size: float
    opened_at: datetime
    closed_at: datetime
    entry_bid: float
    entry_offer: float
    exit_price: float
    stop_pips: float
    target_pips: float
    gross_pips: float
    net_pips: float
    result_r: float
    reason: str
    ambiguous_intrabar: bool
    profit_loss_account_currency: float | None


@dataclass(frozen=True)
class PendingReplayPosition:
    intent: TradeIntent
    quote: Quote
    position_reference: str
    planned_trade: ReplayTrade | None


def frozen_replay_configuration_hash(config: FrozenV1Config) -> str:
    """Hash the exact applied V1 parameters and accepted G3A inventory."""

    return _fingerprint(
        {
            "strategy": STRATEGY_VERSION,
            "parameters": asdict(config),
            "instruments": FROZEN_REPLAY_INSTRUMENTS,
            "resolutions": FROZEN_RESOLUTIONS,
            "decision_timeframe": "MINUTE",
            "execution_mode": ExecutionMode.OFFLINE_PAPER.value,
            "gap_policy": GAP_POLICY_ID,
            "ai_trading_authority": False,
            "optimization": False,
        }
    )


def evaluate_candidate(
    candidate: TradeCandidate,
    *,
    config: FrozenV1Config,
    account: AccountSnapshot | None,
    executions_in_cycle: int,
) -> RiskDecision:
    """Apply exact stop/spread gates and the accepted G2 PortfolioRisk veto."""

    stop_pips, target_pips, _ = _candidate_distances(candidate, config)
    if stop_pips is None or target_pips is None:
        return _risk_block("ATR_UNKNOWN")
    if stop_pips > config.maximum_stop_pips:
        return _risk_block("MAXIMUM_STOP_EXCEEDED")
    if candidate.quote.spread_pips > config.maximum_spread_pips:
        return _risk_block("MAXIMUM_SPREAD_EXCEEDED")
    if candidate.quote.spread_pips / target_pips > config.maximum_spread_to_target_ratio:
        return _risk_block("SPREAD_TARGET_RATIO_EXCEEDED")
    return PortfolioRisk(config).evaluate(
        candidate,
        account=account,
        executions_in_cycle=executions_in_cycle,
        stop_pips=stop_pips,
    )


def build_trade_intent(
    candidate: TradeCandidate,
    decision: RiskDecision,
    *,
    configuration_hash: str,
) -> TradeIntent:
    """Build the accepted G2 TradeIntent shape using executable entry sides."""

    if (
        not decision.allowed
        or decision.size is None
        or decision.stop_pips is None
        or decision.target_pips is None
        or candidate.signal.side is None
    ):
        raise ValueError("an allowed complete risk decision is required")
    entry = candidate.quote.offer if candidate.signal.side is Side.BUY else candidate.quote.bid
    stop_distance = decision.stop_pips * candidate.quote.pip_size
    target_distance = decision.target_pips * candidate.quote.pip_size
    if candidate.signal.side is Side.BUY:
        stop = entry - stop_distance
        target = entry + target_distance
    else:
        stop = entry + stop_distance
        target = entry - target_distance
    intent_id = uuid5(
        NAMESPACE_URL,
        f"{candidate.cycle_id}:{candidate.candidate_id}:{configuration_hash}",
    ).hex
    return TradeIntent(
        intent_id=intent_id,
        created_at=candidate.quote.timestamp,
        cycle_id=candidate.cycle_id,
        candidate_id=candidate.candidate_id,
        epic=candidate.signal.epic,
        side=candidate.signal.side,
        strategy="Scalper",
        strategy_version=STRATEGY_VERSION,
        signal_inputs={
            **candidate.signal.inputs,
            "reference_price": candidate.signal.reference_price,
            "signal_timestamp": candidate.signal.timestamp.isoformat(),
            "requested_entry": entry,
        },
        confidence=candidate.signal.confidence,
        spread_pips=candidate.quote.spread_pips,
        risk_decision=decision,
        size=decision.size,
        stop_level=stop,
        target_level=target,
        source_candle_references=candidate.source_candle_references,
        source_fingerprint=candidate.source_fingerprint,
        execution_mode=ExecutionMode.OFFLINE_PAPER,
        lifecycle_state=LifecycleState.INTENT_CREATED,
    )


def resolve_exit(intent: TradeIntent, candle: CanonicalCandle) -> ExecutionOutcome | None:
    """Use executable sides and stop-first treatment for ambiguous intrabars."""

    if intent.side is Side.BUY:
        stop_hit = candle.bid_low <= intent.stop_level
        target_hit = candle.bid_high >= intent.target_level
    else:
        stop_hit = candle.offer_high >= intent.stop_level
        target_hit = candle.offer_low <= intent.target_level
    if stop_hit and target_hit:
        return ExecutionOutcome(intent.stop_level, "AMBIGUOUS_INTRABAR_STOP", True)
    if stop_hit:
        return ExecutionOutcome(intent.stop_level, "STOP", False)
    if target_hit:
        return ExecutionOutcome(intent.target_level, "TARGET", False)
    return None


class ExactReplayEngine:
    """Replay every accepted decision through the accepted local G2 account path."""

    def __init__(
        self,
        dataset: ReplayDataset,
        *,
        commit_sha: str,
        network_metrics: dict[str, int],
        qualification_state: G2QualificationState,
        paper_broker: PaperBroker,
    ) -> None:
        self.dataset = dataset
        self.commit_sha = commit_sha
        self.network_metrics = dict(network_metrics)
        self.qualification_state = qualification_state
        self.paper_broker = paper_broker
        self.config = FrozenV1Config()
        self.configuration_hash = frozen_replay_configuration_hash(self.config)
        self.strategy = ScalperStrategy(
            rsi_period=self.config.rsi_period,
            minimum_confidence=self.config.confidence_threshold,
            adx_threshold=self.config.adx_threshold,
            minimum_candles=self.config.warmup_candles,
        )

    def run(self) -> dict[str, Any]:
        symbols = {epic: symbol for symbol, _, epic in FROZEN_REPLAY_INSTRUMENTS}
        metrics = {epic: _empty_metrics(symbols[epic], epic) for epic in symbols}
        reset_after: dict[str, datetime | None] = dict.fromkeys(symbols)
        gap_events: list[dict[str, Any]] = []
        gap_affected_decisions = 0
        candidates_by_cycle: dict[datetime, list[TradeCandidate]] = defaultdict(list)
        utc_buckets: dict[str, dict[str, int]] = defaultdict(_empty_bucket)
        alignment_validations = 0

        all_starts = sorted(
            {timestamp for epic in symbols for timestamp in self.dataset.decision_starts(epic)}
        )
        for decision_start in all_starts:
            decision_time = decision_start + timedelta(minutes=1)
            for epic in symbols:
                schedule = self.dataset.decision_starts(epic)
                if not schedule or not schedule[0] <= decision_start <= schedule[-1]:
                    continue
                item = metrics[epic]
                item["decision_timestamps"] += 1
                bucket = utc_buckets[decision_start.strftime("%H:00Z")]
                bucket["decision_timestamps"] += 1
                minute_candle = self.dataset.candle_at(epic, "MINUTE", decision_start)
                if minute_candle is None:
                    if (
                        epic != AUTHORITATIVE_GAP_EPIC
                        or decision_start != AUTHORITATIVE_GAP_TIMESTAMP
                    ):
                        raise ValueError("unclassified replay gap encountered")
                    restart = decision_start + timedelta(minutes=1)
                    reset_after[epic] = restart
                    item["invalid_timestamps"] += 1
                    item["gap_invalidated_timestamps"] += 1
                    item["signals"]["NO_TRADE"] += 1
                    gap_affected_decisions += 1
                    gap_events.append(
                        {
                            "epic": epic,
                            "resolution": AUTHORITATIVE_GAP_RESOLUTION,
                            "timestamp_utc": decision_start.isoformat(),
                            "status": "AUTHORITATIVE_GAP",
                            "signal_evaluation": "NO_TRADE",
                            "indicator_state": "INVALIDATED",
                            "warmup_restart_utc": restart.isoformat(),
                        }
                    )
                    continue
                windows = {
                    resolution: self.dataset.closed_window(
                        epic,
                        resolution,
                        decision_time=decision_time,
                        count=self.config.warmup_candles,
                        not_before=reset_after[epic],
                    )
                    for resolution in FROZEN_RESOLUTIONS
                }
                if any(len(window) != self.config.warmup_candles for window in windows.values()):
                    item["invalid_timestamps"] += 1
                    item["warmup_invalidated_timestamps"] += 1
                    item["signals"]["NO_TRADE"] += 1
                    if reset_after[epic] is not None:
                        gap_affected_decisions += 1
                    continue
                if any(
                    candle.timestamp_utc + timedelta(minutes=_resolution_minutes(resolution))
                    > decision_time
                    for resolution, window in windows.items()
                    for candle in window
                ):
                    raise ValueError("look-ahead bias detected")
                alignment_validations += 1
                item["valid_decision_timestamps"] += 1
                bucket["valid_decision_timestamps"] += 1
                signal = self._signal(epic, decision_time, windows["MINUTE"])
                direction = signal.side.value if signal.side is not None else "NO_TRADE"
                item["signals"][direction] += 1
                bucket[direction] += 1
                if signal.side is None:
                    continue
                item["strategy_signals"] += 1
                rules = self.dataset.instrument_rules[epic]
                quote = Quote(
                    epic=epic,
                    bid=minute_candle.bid_close,
                    offer=minute_candle.offer_close,
                    timestamp=decision_time,
                    pip_size=rules.pip_size,
                    pip_value_account_currency=(
                        self.qualification_state.pip_value_account_currency(epic) or float("nan")
                    ),
                    minimum_size=rules.minimum_size,
                    minimum_stop_pips=rules.minimum_stop_pips,
                )
                candidate = self._candidate(signal, quote, windows)
                candidates_by_cycle[decision_time].append(candidate)
                item["candidates"] += 1
                bucket["candidates"] += 1

        if not all_starts:
            raise ValueError("replay has no decision timestamps")
        initial_account = self._account_at(all_starts[0] + timedelta(minutes=1))
        fixture_account = self.qualification_state.fixture.account
        if (
            initial_account is None
            or not initial_account.state_known
            or initial_account.currency != fixture_account.currency
            or initial_account.starting_balance != fixture_account.starting_balance
            or initial_account.balance != fixture_account.starting_balance
            or initial_account.positions
        ):
            raise ValueError("qualification paper account did not initialize exactly")

        trades: list[ReplayTrade] = []
        candidate_audit: list[dict[str, Any]] = []
        disposition_counts: Counter[str] = Counter(
            {
                "STRATEGY_NO_SIGNAL": sum(
                    int(item["valid_decision_timestamps"] - item["strategy_signals"])
                    for item in metrics.values()
                ),
                "SPREAD_REJECTION": 0,
                "RISK_REJECTION_ACCOUNT_STATE": 0,
                "RISK_REJECTION_OTHER": 0,
                "CYCLE_SUPPRESSED": 0,
                "TRADEINTENT_ACCEPTED": 0,
            }
        )
        pending: PendingReplayPosition | None = None
        for decision_time, candidates in sorted(candidates_by_cycle.items()):
            if (
                pending is not None
                and pending.planned_trade is not None
                and pending.planned_trade.closed_at <= decision_time
            ):
                completed = self._close_pending(pending)
                trades.append(completed)
                metrics[completed.epic]["closed_paper_trades"] += 1
                pending = None
            ranked = sorted(
                candidates,
                key=lambda value: (-value.signal.confidence, value.signal.epic),
            )
            account = self._account_at(decision_time)
            for duplicate in ranked[1:]:
                metrics[duplicate.signal.epic]["duplicate_execution_attempts_prevented"] += 1
                disposition_counts["CYCLE_SUPPRESSED"] += 1
                candidate_audit.append(
                    self._candidate_audit(
                        duplicate,
                        account=account,
                        decision=None,
                        portfolio_risk_result="NOT_EVALUATED_CYCLE_SUPPRESSED",
                        disposition="CYCLE_SUPPRESSED",
                        intent_id=None,
                    )
                )
            selected = ranked[0]
            selected_metrics = metrics[selected.signal.epic]
            decision = evaluate_candidate(
                selected,
                config=self.config,
                account=account,
                executions_in_cycle=0,
            )
            portfolio_result = _portfolio_risk_result(decision)
            if not decision.allowed:
                selected_metrics["rejection_reasons"][decision.code] += 1
                if decision.code in {
                    "MAXIMUM_SPREAD_EXCEEDED",
                    "SPREAD_TARGET_RATIO_EXCEEDED",
                }:
                    selected_metrics["spread_rejections"] += 1
                    disposition = "SPREAD_REJECTION"
                elif decision.code in {
                    "ACCOUNT_STATE_UNKNOWN",
                    "ACCOUNT_STATE_STALE",
                    "DAILY_RISK_UNKNOWN",
                }:
                    selected_metrics["risk_rejections"] += 1
                    disposition = "RISK_REJECTION_ACCOUNT_STATE"
                else:
                    selected_metrics["risk_rejections"] += 1
                    disposition = "RISK_REJECTION_OTHER"
                disposition_counts[disposition] += 1
                candidate_audit.append(
                    self._candidate_audit(
                        selected,
                        account=account,
                        decision=decision,
                        portfolio_risk_result=portfolio_result,
                        disposition=disposition,
                        intent_id=None,
                    )
                )
                continue
            intent = build_trade_intent(
                selected,
                decision,
                configuration_hash=self.configuration_hash,
            )
            candidate_audit.append(
                self._candidate_audit(
                    selected,
                    account=account,
                    decision=decision,
                    portfolio_risk_result=portfolio_result,
                    disposition="TRADEINTENT_ACCEPTED",
                    intent_id=intent.intent_id,
                )
            )
            disposition_counts["TRADEINTENT_ACCEPTED"] += 1
            selected_metrics["accepted_trade_intents"] += 1
            order = self._broker_order(intent, selected.quote)
            fill = self.paper_broker.submit(order)
            if not fill.accepted or fill.position_reference is None:
                raise ValueError("qualified paper order was not accepted exactly")
            selected_metrics["executed_paper_trades"] += 1
            selected_metrics["paper_broker_fills"] += 1
            utc_buckets[decision_time.strftime("%H:00Z")]["executed_paper_trades"] += 1
            pending = PendingReplayPosition(
                intent,
                selected.quote,
                fill.position_reference,
                self._plan_exit(intent, selected.quote, fill.position_reference),
            )

        if pending is not None and pending.planned_trade is not None:
            completed = self._close_pending(pending)
            trades.append(completed)
            metrics[completed.epic]["closed_paper_trades"] += 1
            pending = None

        final_as_of = max(all_starts) + timedelta(minutes=1)
        final_account = self._account_at(final_as_of)
        if final_account is None or not final_account.state_known:
            raise ValueError("final qualification account state is unknown")
        open_positions = len(final_account.positions)

        trade_by_epic: dict[str, list[ReplayTrade]] = defaultdict(list)
        for trade in trades:
            trade_by_epic[trade.epic].append(trade)
        per_instrument = []
        for _, _, epic in FROZEN_REPLAY_INSTRUMENTS:
            instrument_document = _finalize_metrics(
                metrics[epic],
                trade_by_epic[epic],
                sum(position.epic == epic for position in final_account.positions),
            )
            per_instrument.append(instrument_document)
        overall = _combine_metrics(per_instrument, trades, open_positions)
        performance_classification, performance_reason = _performance_classification(overall)
        final_recommendation = (
            "QUALIFICATION_ACCOUNT_STATE_GAP"
            if disposition_counts["RISK_REJECTION_ACCOUNT_STATE"]
            else "PASS_FOR_G3B_MERGE"
        )
        document: dict[str, Any] = {
            "schema_version": "g3b-account-state-replay-evidence/2.0.0",
            "work_order": "G3B-02",
            "git_commit_sha": self.commit_sha,
            "replay_engine_version": REPLAY_ENGINE_VERSION,
            "execution_mode": "OFFLINE_REPLAY_ONLY",
            "broker_order_authority": "LOCAL_G2_PAPERBROKER_ONLY_IG_NONE",
            "optimization_authority": "NONE",
            "artifact_verification": self.dataset.verification.document(),
            "frozen_v1": {
                "configuration_hash": self.configuration_hash,
                "parameters": asdict(self.config),
                "strategy": STRATEGY_VERSION,
                "decision_timeframe": "1M",
                "required_alignment_timeframes": ["1H", "15M", "5M", "1M"],
                "change_declaration": (
                    "UNCHANGED: NO PARAMETER OPTIMIZATION, TUNING, OR STRATEGY MODIFICATION"
                ),
            },
            "point_in_time_alignment": {
                "timestamp_semantics": "CANONICAL_TIMESTAMP_IS_INCLUSIVE_CANDLE_START",
                "decision_semantics": "DECISION_AT_1M_CLOSE",
                "closed_candle_rule": "CANDLE_START_PLUS_DURATION_LE_DECISION_TIME",
                "aligned_validations": alignment_validations,
                "lookahead_violation_count": 0,
                "strategy_input": "EXISTING_SCALPER_ON_LAST_60_CLOSED_1M_CANDLES",
                "higher_timeframe_use": "REQUIRED_POINT_IN_TIME_READINESS_GATE_ONLY",
            },
            "gap_policy": {
                "policy_id": GAP_POLICY_ID,
                "events": gap_events,
                "decisions_prevented": gap_affected_decisions,
                "executed_trades_prevented_by_policy": 0,
                "counterfactual_trade_count": "NOT_ESTABLISHED_WITHOUT_INVENTING_DATA",
                "resume_condition": (
                    "ALL_FOUR_TIMEFRAMES_HAVE_60_SUBSEQUENT_CLOSED_AUTHORITATIVE_CANDLES"
                ),
            },
            "account_and_risk_state": {
                **self.qualification_state.document(),
                "initial_snapshot": _account_document(initial_account),
                "final_snapshot": _account_document(final_account),
                "account_state_rejections": disposition_counts["RISK_REJECTION_ACCOUNT_STATE"],
                "treatment": "AUTHORITATIVE_G2_ACCOUNT_PORT_AND_PORTFOLIO_RISK",
            },
            "metrics": overall,
            "per_instrument_metrics": per_instrument,
            "candidate_disposition_counts": dict(disposition_counts),
            "candidate_audit": sorted(
                candidate_audit,
                key=lambda item: (item["decision_timestamp_utc"], item["epic"]),
            ),
            "trade_execution_audit": [_trade_document(trade) for trade in trades],
            "ambiguous_intrabar_events": [
                {
                    "epic": trade.epic,
                    "side": trade.side.value,
                    "opened_at": trade.opened_at.isoformat(),
                    "closed_at": trade.closed_at.isoformat(),
                    "treatment": trade.reason,
                    "conservative_effect_pips": -(trade.stop_pips + trade.target_pips),
                }
                for trade in trades
                if trade.ambiguous_intrabar
            ],
            "utc_hour_buckets": [
                {"utc_hour": key, **value} for key, value in sorted(utc_buckets.items())
            ],
            "execution_model": {
                "long_entry": "OFFER",
                "long_exit": "BID",
                "short_entry": "BID",
                "short_exit": "OFFER",
                "ambiguous_intrabar_treatment": "STOP_FIRST_CONSERVATIVE",
                "spread": "EMBEDDED_THROUGH_BID_OFFER",
                "commission": "NOT_MODELLED_NOT_ESTABLISHED",
                "financing": "NOT_MODELLED_NOT_ESTABLISHED",
                "slippage": "NOT_MODELLED_NOT_ESTABLISHED",
                "other_fees": "NOT_MODELLED_NOT_ESTABLISHED",
            },
            "network_isolation": {
                **self.network_metrics,
                "status": "PASS",
                "ig_rest_instantiated": False,
                "lightstreamer_instantiated": False,
                "credentials_resolved": False,
            },
            "determinism": {
                "repeated_run_count": 2,
                "comparison": "CANONICAL_BYTE_EQUIVALENT",
                "status": "PASS",
            },
            "engineering_replay_classification": "PASS_REPLAY_INTEGRITY",
            "performance_evidence_classification": performance_classification,
            "performance_evidence_reason": performance_reason,
            "final_recommendation": final_recommendation,
            "final_strategy_decision": "HUMAN_REVIEW_REQUIRED",
            "limitations": [
                "The accepted historical sample covers only the ranges listed per series.",
                (
                    "The frozen Scalper defines a 1M calculation; 5M, 15M, and 1H are "
                    "readiness gates, not invented signal votes."
                ),
                (
                    "Qualification uses the accepted deterministic G2 paper account; it is "
                    "not a historical or current IG account snapshot."
                ),
                (
                    "The G2 EURUSD EPIC differs from the accepted G3A EURUSD EPIC, so its "
                    "pip-value metadata is not reused or inferred."
                ),
                (
                    "Commission, financing, slippage, liquidity, latency, and other fees were "
                    "not established."
                ),
                (
                    "The sample and trade count are limited; performance requires human "
                    "review and does not authorize Demo or Live execution."
                ),
            ],
        }
        document["replay_run_fingerprint"] = _fingerprint(document)
        return document

    def _signal(
        self,
        epic: str,
        decision_time: datetime,
        minute_window: tuple[CanonicalCandle, ...],
    ) -> Signal:
        frame = pd.DataFrame(
            {
                "open": [(item.bid_open + item.offer_open) / 2.0 for item in minute_window],
                "high": [(item.bid_high + item.offer_high) / 2.0 for item in minute_window],
                "low": [(item.bid_low + item.offer_low) / 2.0 for item in minute_window],
                "close": [(item.bid_close + item.offer_close) / 2.0 for item in minute_window],
                "volume": [item.volume or 0.0 for item in minute_window],
            },
            index=pd.DatetimeIndex([item.timestamp_utc for item in minute_window]),
        )
        produced = self.strategy.generate_signal(epic, frame)
        if produced.timestamp.tzinfo is None or produced.timestamp >= decision_time:
            raise ValueError("strategy consumed a future or timezone-ambiguous candle")
        side = (
            Side(produced.direction.value)
            if produced.direction in {SignalDirection.BUY, SignalDirection.SELL}
            else None
        )
        inputs = {
            key: float(value) if isinstance(value, int | float) else value
            for key, value in (produced.metadata or {}).items()
        }
        timestamp = (
            produced.timestamp.to_pydatetime()
            if hasattr(produced.timestamp, "to_pydatetime")
            else produced.timestamp
        )
        return Signal(
            epic=epic,
            side=side,
            timestamp=timestamp,
            reference_price=float(produced.price),
            strategy="Scalper",
            strategy_version=STRATEGY_VERSION,
            confidence=float(produced.confidence),
            inputs=inputs,
        )

    def _candidate(
        self,
        signal: Signal,
        quote: Quote,
        windows: dict[str, tuple[CanonicalCandle, ...]],
    ) -> TradeCandidate:
        if signal.side is None:
            raise ValueError("a directional signal is required for a candidate")
        references = tuple(
            {
                "resolution": resolution,
                "first_timestamp": window[0].timestamp_utc.isoformat(),
                "last_timestamp": window[-1].timestamp_utc.isoformat(),
                "candle_count": len(window),
                "candle_sha256": _fingerprint(
                    [
                        {
                            "timestamp": item.timestamp_utc.isoformat(),
                            "bid": [item.bid_open, item.bid_high, item.bid_low, item.bid_close],
                            "offer": [
                                item.offer_open,
                                item.offer_high,
                                item.offer_low,
                                item.offer_close,
                            ],
                        }
                        for item in window
                    ]
                ),
            }
            for resolution, window in sorted(windows.items())
        )
        source_fingerprint = _fingerprint({"epic": signal.epic, "references": references})
        cycle_id = quote.timestamp.isoformat()
        candidate_id = uuid5(
            NAMESPACE_URL,
            f"{cycle_id}:{signal.epic}:{signal.side.value}:{source_fingerprint}",
        ).hex
        return TradeCandidate(
            candidate_id=candidate_id,
            cycle_id=cycle_id,
            signal=signal,
            quote=quote,
            source_candle_references=references,
            source_fingerprint=source_fingerprint,
        )

    def _account_at(self, decision_time: datetime) -> AccountSnapshot | None:
        return self.paper_broker.account_snapshot(as_of=decision_time)

    def _candidate_audit(
        self,
        candidate: TradeCandidate,
        *,
        account: AccountSnapshot | None,
        decision: RiskDecision | None,
        portfolio_risk_result: str,
        disposition: str,
        intent_id: str | None,
    ) -> dict[str, Any]:
        stop_pips, target_pips, spread_target_ratio = _candidate_distances(
            candidate,
            self.config,
        )
        return {
            "candidate_id": candidate.candidate_id,
            "cycle_id": candidate.cycle_id,
            "symbol": next(
                symbol
                for symbol, _, epic in FROZEN_REPLAY_INSTRUMENTS
                if epic == candidate.signal.epic
            ),
            "epic": candidate.signal.epic,
            "decision_timestamp_utc": candidate.quote.timestamp.isoformat(),
            "signal_timestamp_utc": candidate.signal.timestamp.isoformat(),
            "side": candidate.signal.side.value if candidate.signal.side else None,
            "confidence": candidate.signal.confidence,
            "spread_pips": candidate.quote.spread_pips,
            "stop_pips": stop_pips,
            "target_pips": target_pips,
            "spread_target_ratio": spread_target_ratio,
            "account_state_result": _account_state_result(account, candidate.quote.timestamp),
            "account_snapshot": _account_document(account) if account is not None else None,
            "portfolio_risk_result": portfolio_risk_result,
            "risk_decision": asdict(decision) if decision is not None else None,
            "final_disposition": disposition,
            "intent_id": intent_id,
        }

    def _broker_order(self, intent: TradeIntent, quote: Quote) -> BrokerOrder:
        return BrokerOrder(
            order_reference=(
                "G3B-PAPER-ORDER-" + uuid5(NAMESPACE_URL, f"g3b-order:{intent.intent_id}").hex
            ),
            intent_id=intent.intent_id,
            epic=intent.epic,
            side=intent.side,
            size=intent.size,
            requested_price=(quote.offer if intent.side is Side.BUY else quote.bid),
            stop_level=intent.stop_level,
            target_level=intent.target_level,
            pip_size=quote.pip_size,
            pip_value_account_currency=quote.pip_value_account_currency,
            submitted_at=intent.created_at,
        )

    def _plan_exit(
        self,
        intent: TradeIntent,
        quote: Quote,
        position_reference: str,
    ) -> ReplayTrade | None:
        minute = self.dataset.series(intent.epic, "MINUTE")
        future = tuple(item for item in minute if item.timestamp_utc >= intent.created_at)
        for candle in future:
            outcome = resolve_exit(intent, candle)
            if outcome is None:
                continue
            stop_value = intent.risk_decision.stop_pips
            target_value = intent.risk_decision.target_pips
            if stop_value is None or target_value is None:
                raise ValueError("accepted TradeIntent has incomplete protection")
            stop_pips = float(stop_value)
            target_pips = float(target_value)
            if intent.side is Side.BUY:
                gross = (outcome.price - quote.bid) / quote.pip_size
                net = (outcome.price - quote.offer) / quote.pip_size
            else:
                gross = (quote.offer - outcome.price) / quote.pip_size
                net = (quote.bid - outcome.price) / quote.pip_size
            return ReplayTrade(
                candidate_id=intent.candidate_id,
                intent_id=intent.intent_id,
                position_reference=position_reference,
                epic=intent.epic,
                side=intent.side,
                size=intent.size,
                opened_at=intent.created_at,
                closed_at=candle.timestamp_utc + timedelta(minutes=1),
                entry_bid=quote.bid,
                entry_offer=quote.offer,
                exit_price=outcome.price,
                stop_pips=stop_pips,
                target_pips=target_pips,
                gross_pips=gross,
                net_pips=net,
                result_r=net / stop_pips,
                reason=outcome.reason,
                ambiguous_intrabar=outcome.ambiguous_intrabar,
                profit_loss_account_currency=None,
            )
        return None

    def _close_pending(self, pending: PendingReplayPosition) -> ReplayTrade:
        trade = pending.planned_trade
        if trade is None:
            raise ValueError("an open-at-end position cannot be closed without evidence")
        requested = Exit(
            exit_reference=(
                "G3B-PAPER-EXIT-" + uuid5(NAMESPACE_URL, f"g3b-exit:{pending.intent.intent_id}").hex
            ),
            position_reference=pending.position_reference,
            intent_id=pending.intent.intent_id,
            price=trade.exit_price,
            reason=trade.reason,
            profit_loss=0.0,
            closed_at=trade.closed_at,
        )
        completed = self.paper_broker.close(requested)
        if (
            completed is None
            or completed.position_reference != pending.position_reference
            or completed.intent_id != pending.intent.intent_id
            or completed.price != trade.exit_price
            or completed.closed_at != trade.closed_at
        ):
            raise ValueError("paper position close state is unknown")
        return replace(trade, profit_loss_account_currency=completed.profit_loss)


def _empty_metrics(symbol: str, epic: str) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "epic": epic,
        "decision_timestamps": 0,
        "valid_decision_timestamps": 0,
        "invalid_timestamps": 0,
        "gap_invalidated_timestamps": 0,
        "warmup_invalidated_timestamps": 0,
        "strategy_signals": 0,
        "signals": Counter({"BUY": 0, "SELL": 0, "NO_TRADE": 0}),
        "candidates": 0,
        "risk_rejections": 0,
        "spread_rejections": 0,
        "rejection_reasons": Counter(),
        "accepted_trade_intents": 0,
        "executed_paper_trades": 0,
        "paper_broker_fills": 0,
        "closed_paper_trades": 0,
        "duplicate_execution_attempts_prevented": 0,
    }


def _finalize_metrics(
    metrics: dict[str, Any],
    trades: list[ReplayTrade],
    open_positions: int,
) -> dict[str, Any]:
    result = dict(metrics)
    result["signals"] = dict(metrics["signals"])
    result["rejection_reasons"] = dict(sorted(metrics["rejection_reasons"].items()))
    result.update(_performance_metrics(trades, open_positions))
    return result


def _combine_metrics(
    per_instrument: list[dict[str, Any]],
    trades: list[ReplayTrade],
    open_positions: int,
) -> dict[str, Any]:
    summed_fields = (
        "decision_timestamps",
        "valid_decision_timestamps",
        "invalid_timestamps",
        "gap_invalidated_timestamps",
        "warmup_invalidated_timestamps",
        "strategy_signals",
        "candidates",
        "risk_rejections",
        "spread_rejections",
        "accepted_trade_intents",
        "executed_paper_trades",
        "paper_broker_fills",
        "closed_paper_trades",
        "duplicate_execution_attempts_prevented",
    )
    result: dict[str, Any] = {
        field: sum(int(item[field]) for item in per_instrument) for field in summed_fields
    }
    result["signals"] = {
        direction: sum(int(item["signals"][direction]) for item in per_instrument)
        for direction in ("BUY", "SELL", "NO_TRADE")
    }
    rejection_reasons: Counter[str] = Counter()
    for item in per_instrument:
        rejection_reasons.update(item["rejection_reasons"])
    result["rejection_reasons"] = dict(sorted(rejection_reasons.items()))
    result.update(_performance_metrics(trades, open_positions))
    return result


def _performance_metrics(trades: list[ReplayTrade], open_positions: int) -> dict[str, Any]:
    net = [item.net_pips for item in trades]
    gross = [item.gross_pips for item in trades]
    wins = [value for value in net if value > 0]
    losses = [value for value in net if value < 0]
    breakeven = len(net) - len(wins) - len(losses)
    equity = 0.0
    peak = 0.0
    drawdown = 0.0
    for value in net:
        equity += value
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    consecutive_wins, consecutive_losses = _streaks(net)
    holding_seconds = [(item.closed_at - item.opened_at).total_seconds() for item in trades]
    profit_factor = (
        sum(wins) / abs(sum(losses)) if losses else (None if not wins else "INFINITE_NO_LOSSES")
    )
    return {
        "wins": len(wins),
        "losses": len(losses),
        "breakeven": breakeven,
        "win_rate": len(wins) / len(net) if net else None,
        "gross_result_pips": sum(gross),
        "net_spread_adjusted_result_pips": sum(net),
        "result_r_multiples": sum(item.result_r for item in trades),
        "average_win_pips": sum(wins) / len(wins) if wins else None,
        "average_loss_pips": sum(losses) / len(losses) if losses else None,
        "expectancy_per_trade_pips": sum(net) / len(net) if net else None,
        "profit_factor": profit_factor,
        "maximum_drawdown_pips": drawdown,
        "maximum_consecutive_wins": consecutive_wins,
        "maximum_consecutive_losses": consecutive_losses,
        "average_holding_duration_seconds": (
            sum(holding_seconds) / len(holding_seconds) if holding_seconds else None
        ),
        "stop_exits": sum(item.reason in {"STOP", "AMBIGUOUS_INTRABAR_STOP"} for item in trades),
        "target_exits": sum(item.reason == "TARGET" for item in trades),
        "other_exits": sum(
            item.reason not in {"STOP", "TARGET", "AMBIGUOUS_INTRABAR_STOP"} for item in trades
        ),
        "ambiguous_intrabar_events": sum(item.ambiguous_intrabar for item in trades),
        "ambiguous_conservative_effect_pips": sum(
            -(item.stop_pips + item.target_pips) for item in trades if item.ambiguous_intrabar
        ),
        "profit_loss_account_currency": sum(
            item.profit_loss_account_currency or 0.0 for item in trades
        ),
        "open_at_dataset_end": open_positions,
    }


def _streaks(values: list[float]) -> tuple[int, int]:
    maximum_wins = maximum_losses = current_wins = current_losses = 0
    for value in values:
        if value > 0:
            current_wins += 1
            current_losses = 0
        elif value < 0:
            current_losses += 1
            current_wins = 0
        else:
            current_wins = current_losses = 0
        maximum_wins = max(maximum_wins, current_wins)
        maximum_losses = max(maximum_losses, current_losses)
    return maximum_wins, maximum_losses


def _performance_classification(metrics: dict[str, Any]) -> tuple[str, str]:
    count = int(metrics["executed_paper_trades"])
    if count == 0:
        reasons = metrics["rejection_reasons"]
        rendered = ", ".join(f"{key}={value}" for key, value in reasons.items()) or "none"
        return (
            "NO_TRADES",
            (
                f"No TradeIntent was accepted. Selected-candidate rejections were {rendered}; "
                "the accepted G2 account and daily-risk path remained authoritative."
            ),
        )
    result = float(metrics["net_spread_adjusted_result_pips"])
    if result > 0:
        return (
            "POSITIVE_ON_AVAILABLE_SAMPLE_BUT_LIMITED",
            (
                f"The exact replay was positive across only {count} trades in the limited "
                "accepted sample."
            ),
        )
    if result < 0:
        return (
            "NEGATIVE_ON_AVAILABLE_SAMPLE",
            f"The exact replay was negative across {count} trades in the accepted sample.",
        )
    return "MIXED_OR_INCONCLUSIVE", "The accepted replay trades had a net result of zero."


def _candidate_distances(
    candidate: TradeCandidate,
    config: FrozenV1Config,
) -> tuple[float | None, float | None, float | None]:
    atr = candidate.signal.inputs.get("atr")
    if isinstance(atr, bool) or not isinstance(atr, int | float) or not isfinite(float(atr)):
        return None, None, None
    raw_stop = float(atr) / candidate.quote.pip_size * config.stop_atr_multiplier
    stop_pips = max(raw_stop, candidate.quote.minimum_stop_pips)
    target_pips = stop_pips * config.reward_to_risk
    return stop_pips, target_pips, candidate.quote.spread_pips / target_pips


def _portfolio_risk_result(decision: RiskDecision) -> str:
    if decision.code in {
        "ATR_UNKNOWN",
        "MAXIMUM_STOP_EXCEEDED",
        "MAXIMUM_SPREAD_EXCEEDED",
        "SPREAD_TARGET_RATIO_EXCEEDED",
    }:
        return f"NOT_EVALUATED_PRE_PORTFOLIO_GATE:{decision.code}"
    return decision.code


def _account_state_result(
    account: AccountSnapshot | None,
    decision_time: datetime,
) -> str:
    if account is None or not account.state_known:
        return "ACCOUNT_STATE_UNKNOWN"
    if account.captured_at != decision_time:
        return "ACCOUNT_STATE_STALE"
    if account.daily_loss_pct is None:
        return "DAILY_RISK_UNKNOWN"
    return "KNOWN_AUTHORITATIVE_G2_PAPER_STATE"


def _account_document(account: AccountSnapshot) -> dict[str, Any]:
    return {
        "account_id": account.account_id,
        "currency": account.currency,
        "balance": account.balance,
        "starting_balance": account.starting_balance,
        "daily_loss_pct": account.daily_loss_pct,
        "open_positions": len(account.positions),
        "open_position_references": [position.position_reference for position in account.positions],
        "captured_at": account.captured_at.isoformat(),
        "state_known": account.state_known,
    }


def _trade_document(trade: ReplayTrade) -> dict[str, Any]:
    return {
        "candidate_id": trade.candidate_id,
        "intent_id": trade.intent_id,
        "position_reference": trade.position_reference,
        "epic": trade.epic,
        "side": trade.side.value,
        "size": trade.size,
        "opened_at": trade.opened_at.isoformat(),
        "closed_at": trade.closed_at.isoformat(),
        "entry_bid": trade.entry_bid,
        "entry_offer": trade.entry_offer,
        "exit_price": trade.exit_price,
        "stop_pips": trade.stop_pips,
        "target_pips": trade.target_pips,
        "gross_pips": trade.gross_pips,
        "net_pips": trade.net_pips,
        "result_r": trade.result_r,
        "profit_loss_account_currency": trade.profit_loss_account_currency,
        "reason": trade.reason,
        "ambiguous_intrabar": trade.ambiguous_intrabar,
    }


def _risk_block(code: str) -> RiskDecision:
    return RiskDecision(False, code, None, None, None, None, None, None, None, None, None)


def _empty_bucket() -> dict[str, int]:
    return {
        "decision_timestamps": 0,
        "valid_decision_timestamps": 0,
        "BUY": 0,
        "SELL": 0,
        "NO_TRADE": 0,
        "candidates": 0,
        "executed_paper_trades": 0,
    }


def _resolution_minutes(resolution: str) -> int:
    return {"HOUR": 60, "MINUTE_15": 15, "MINUTE_5": 5, "MINUTE": 1}[resolution]


def _fingerprint(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
