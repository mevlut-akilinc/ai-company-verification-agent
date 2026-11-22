"""Central configuration. Values come from environment variables (.env supported)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class Paths:
    data: Path = field(default_factory=lambda: Path(_env("KYB_DATA_DIR", str(PROJECT_ROOT / "data"))))

    @property
    def raw(self) -> Path:
        return self.data / "raw"

    @property
    def processed(self) -> Path:
        return self.data / "processed"

    @property
    def eval(self) -> Path:
        return self.data / "eval"

    def ensure(self) -> "Paths":
        for p in (self.raw, self.processed, self.eval):
            p.mkdir(parents=True, exist_ok=True)
        return self


@dataclass(frozen=True)
class Neo4jSettings:
    uri: str = field(default_factory=lambda: _env("NEO4J_URI", "bolt://localhost:7687"))
    user: str = field(default_factory=lambda: _env("NEO4J_USER", "neo4j"))
    password: str = field(default_factory=lambda: _env("NEO4J_PASSWORD", "kyb-dev-password"))
    database: str = field(default_factory=lambda: _env("NEO4J_DATABASE", "neo4j"))


# GLEIF registration-authority code for UK Companies House.
GLEIF_RA_COMPANIES_HOUSE = "RA000585"

# Ownership threshold for beneficial ownership (percent). Kept configurable:
# UK PSC and the Turkish MASAK rule both use 25%, but this must not be hard-coded.
UBO_THRESHOLD_PCT = float(_env("KYB_UBO_THRESHOLD_PCT", "25"))
