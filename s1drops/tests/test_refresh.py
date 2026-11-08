"""The monthly update: new passes merged in, checked, then switched to; the old
version kept aside; nothing changed when there's nothing new or a check fails."""
import time
from datetime import date
from pathlib import Path

import numpy as np
import pytest

from s1drops.app import share
from s1drops.cube import refresh
from s1drops.cube.cache import read_cube, write_cube
from s1drops.cube.registry import list_cubes, register_cube
from s1drops.tests.test_app import _cube

KEY = "lagos_100m_2024-01-01_2024-08-01_nm"


@pytest.fixture
def area(tmp_path):
    cube = _cube(n=16)                   # 16 passes, 12 days apart: 1 Jan - 29 Jun 2024
    cube.attrs.update({"name": "lagos_100m", "start": "2024-01-01", "end": "2024-08-01",
                       "reduction": "native_median", "bbox_ll": [3.40, 6.45, 3.45, 6.50]})
    path = write_cube(cube, tmp_path / f"{KEY}.zarr")
    register_cube(tmp_path, path)
    return tmp_path


def _new_passes(n):
    """A fake build_cube returning `n` passes after the area's end."""
    def build(bbox, start, end, **kw):
        part = _cube(n=16).isel(time=slice(0, n))
        days = np.datetime64(start) + np.arange(n) * np.timedelta64(6, "D")
        return part.assign_coords(time=days.astype("datetime64[ns]")), "utm"
    return build


def test_update_adds_passes_and_keeps_the_old_version(area, monkeypatch):
    monkeypatch.setattr(refresh, "build_cube", _new_passes(3))
    msg = refresh.update_area(list_cubes(area)[0], area, today=date(2024, 9, 1))
    assert "added 3 new passes" in msg
    (entry,) = list_cubes(area)                       # one area, under its new name
    assert entry.key == "lagos_100m_2024-01-01_2024-08-29_nm"
    assert entry.n_passes == 19
    cube = read_cube(entry.path)
    assert "aoi_mask" in cube and not bool(cube["aoi_mask"].values[0, 0])   # outline kept
    assert (area / "previous" / f"{KEY}.zarr").exists()                    # old kept aside
    assert not (area / f"{KEY}.zarr").exists()
    # Links keep working across the update: the area is named without dates.
    assert refresh.area_id(entry.key) == refresh.area_id(KEY) == "lagos_100m_nm"
    assert share.find_area(KEY, [entry.key]) == entry.key


def test_nothing_new_or_up_to_date_changes_nothing(area, monkeypatch):
    def none_yet(*a, **kw):
        raise RuntimeError("No Sentinel-1 scenes for ... over ...")
    monkeypatch.setattr(refresh, "build_cube", none_yet)
    entry = list_cubes(area)[0]
    assert "no new passes yet" in refresh.update_area(entry, area, today=date(2024, 9, 1))
    assert "already up to date" in refresh.update_area(entry, area, today=date(2024, 8, 2))
    assert [e.key for e in list_cubes(area)] == [KEY]


def test_failed_check_leaves_the_area_as_it_was(area, monkeypatch):
    def empty(bbox, start, end, **kw):
        part, _ = _new_passes(2)(bbox, start, end)
        part["vv"] = part["vv"] * np.nan                  # passes without data
        return part, "utm"
    monkeypatch.setattr(refresh, "build_cube", empty)
    results = refresh.update_all(area, today=date(2024, 9, 1), trigger="manual")
    assert "not updated" in results[0] and "no data" in results[0]
    assert [e.key for e in list_cubes(area)] == [KEY]
    assert not any(Path(area).glob("lagos_100m_2024-01-01_2024-08-29_nm.zarr"))
    state = refresh.load_state(area)
    assert state["history"][0]["trigger"] == "manual"


def test_schedule_waits_for_the_next_month(tmp_path):
    nov9 = time.mktime((2026, 11, 9, 12, 0, 0, 0, 0, 0))
    assert refresh.due(tmp_path, now=nov9) is False                     # first start: noted
    assert refresh.due(tmp_path, now=nov9 + 5 * 86400) is False         # same month
    dec1 = time.mktime((2026, 12, 1, 3, 0, 0, 0, 0, 0))
    assert refresh.due(tmp_path, now=dec1) is True
