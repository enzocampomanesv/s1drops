from datetime import datetime
from types import SimpleNamespace

import pytest

from s1drops.cube import build


def _meta(ro, day, state="ascending", pols=("vv", "vh")):
    item = SimpleNamespace(datetime=datetime(2024, 1, day), id=f"{ro}-{day}")
    return SimpleNamespace(item=item, relative_orbit=ro, orbit_state=state, pols=pols)


def test_estimate_cube_bytes_math():
    # cells x timesteps x bands x 4 bytes x 1.3 headroom
    assert build.estimate_cube_bytes(100, 10, 2) == pytest.approx(100 * 10 * 2 * 4 * 1.3)


def test_estimate_timesteps_counts_orbit_day_pairs():
    metas = [
        _meta(1, 1), _meta(1, 1),   # same orbit+day -> one slice (mosaicked)
        _meta(1, 13),               # same orbit, later day -> another
        _meta(2, 1),                # different orbit, same day -> another
    ]
    assert build.estimate_timesteps(metas) == 3


def test_build_cube_refuses_over_budget(monkeypatch):
    # No network: fake the STAC search; the guard must fire before any load.
    monkeypatch.setattr(build, "search_items", lambda *a, **k: [_meta(1, 1), _meta(1, 13)])
    with pytest.raises(RuntimeError, match="budget"):
        build.build_cube(
            (0.0, 0.0, 0.1, 0.1), "2024-01-01", "2024-02-01",
            resolution=10.0, catalog=object(), max_bytes=1.0,  # 1 byte => always exceeds
        )
