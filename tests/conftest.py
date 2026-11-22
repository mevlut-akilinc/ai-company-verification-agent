import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from make_fixtures import write_fixtures  # noqa: E402

from kyb.config import Paths  # noqa: E402
from kyb.data.subset import SubsetParams, build_subset  # noqa: E402


@pytest.fixture(scope="session")
def built(tmp_path_factory):
    data = tmp_path_factory.mktemp("data")
    paths = Paths(data=data).ensure()
    write_fixtures(paths.raw)
    params = SubsetParams(target=15, min_chain_depth=2, distractor_leis=5, cycle_budget_ratio=0.2)
    manifest = build_subset(paths, params)
    return paths, manifest
