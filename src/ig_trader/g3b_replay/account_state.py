"""Accepted G2 qualification account state for the frozen G3B replay."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.ig_trader.g3b_replay.data import FROZEN_REPLAY_INSTRUMENTS
from src.ig_trader.offline_paper.fixture import LocalFixtureData
from src.ig_trader.offline_paper.paper_broker import PaperBroker

ACCEPTED_G2_COMMIT_SHA = "4bc3de5b03eedcfcbb71d3f7042047533c4ca75b"
ACCEPTED_G2_FIXTURE_SHA256 = "ee1a15853e77e2a9aece0a88a623ea378bf976d45d00a44610ae4e53a1d6ac2d"


class QualificationAccountStateGap(ValueError):
    """The accepted deterministic qualification state is absent or unsuitable."""


@dataclass(frozen=True)
class G2QualificationState:
    """Verified deterministic state reused through G2's AccountPort implementation."""

    fixture: LocalFixtureData
    account_state_hash: str

    @classmethod
    def load(cls, path: str | Path) -> G2QualificationState:
        try:
            fixture = LocalFixtureData(path)
        except (OSError, TypeError, ValueError) as error:
            raise QualificationAccountStateGap(
                "accepted G2 qualification fixture is unavailable"
            ) from error
        if fixture.document_fingerprint != ACCEPTED_G2_FIXTURE_SHA256:
            raise QualificationAccountStateGap(
                "qualification fixture differs from accepted G2 state"
            )
        account = fixture.account
        state = {
            "account_id": account.account_id,
            "currency": account.currency,
            "starting_balance": account.starting_balance,
            "initial_balance": account.starting_balance,
            "initial_positions": [],
            "state_known": True,
            "source_fixture_sha256": fixture.document_fingerprint,
        }
        return cls(fixture, _fingerprint(state))

    def create_paper_broker(self, path: str | Path) -> PaperBroker:
        account = self.fixture.account
        return PaperBroker(
            path,
            account_id=account.account_id,
            currency=account.currency,
            starting_balance=account.starting_balance,
        )

    def pip_value_account_currency(self, epic: str) -> float | None:
        """Return G2 metadata only for an exact EPIC match; never infer a conversion."""

        instrument = self.fixture.instrument(epic)
        return instrument.pip_value_account_currency if instrument is not None else None

    def document(self) -> dict[str, Any]:
        account = self.fixture.account
        instrument_sources = []
        for symbol, _, epic in FROZEN_REPLAY_INSTRUMENTS:
            instrument = self.fixture.instrument(epic)
            instrument_sources.append(
                {
                    "symbol": symbol,
                    "g3b_epic": epic,
                    "status": (
                        "AVAILABLE_EXACT_EPIC" if instrument else "UNAVAILABLE_EPIC_MISMATCH"
                    ),
                    "pip_value_account_currency": (
                        instrument.pip_value_account_currency if instrument else None
                    ),
                }
            )
        return {
            "status": "KNOWN_DETERMINISTIC_OFFLINE_PAPER",
            "accepted_g2_commit_sha": ACCEPTED_G2_COMMIT_SHA,
            "fixture_sha256": self.fixture.document_fingerprint,
            "qualification_account_state_hash": self.account_state_hash,
            "account_port": "offline_paper.paper_broker.PaperBroker.account_snapshot",
            "portfolio_risk": "offline_paper.conductor.PortfolioRisk.evaluate",
            "account": {
                "account_id": account.account_id,
                "currency": account.currency,
                "starting_balance": account.starting_balance,
                "initial_balance": account.starting_balance,
                "initial_positions": 0,
                "state_known": True,
            },
            "instrument_risk_metadata": instrument_sources,
            "suitability": (
                "SUITABLE_FOR_ORIGINAL_ACCOUNT_BLOCKED_GBPUSD_CANDIDATES; "
                "EURUSD_PIP_VALUE_REMAINS_FAIL_CLOSED_ON_EPIC_MISMATCH"
            ),
            "prohibited_substitutions": [
                "NO_G2_STOP_DISTANCE_SUBSTITUTION",
                "NO_EURUSD_EPIC_OR_PIP_VALUE_SUBSTITUTION",
                "NO_HISTORICAL_ACCOUNT_BACKFILL_OR_RESULT_TUNING",
            ],
        }


def _fingerprint(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
