import csv
import io
import json
import zipfile

import numpy as np

from s1drops.app import downloads, validation
from s1drops.tests.test_clusters import _grid


def _items():
    return [downloads.Item("a", "Area", "A", "a.csv", lambda: "x,y\n1,2\n"),
            downloads.Item("b", "Area", "B", "b.pdf", lambda: b"%PDF-1.4"),
            downloads.Item("c", "Change", "C", "c.tif", None, "fetch photos first")]


def test_one_item_downloads_as_itself_several_as_a_zip():
    a, b, _ = _items()
    assert downloads.bundle([a], "all.zip") == ("a.csv", b"x,y\n1,2\n")
    name, data = downloads.bundle([a, b], "all.zip")
    assert name == "all.zip"
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        assert sorted(z.namelist()) == ["a.csv", "b.pdf"]
        assert z.read("b.pdf") == b"%PDF-1.4"


def test_changes_table_and_outlines_carry_confidence(tmp_path):
    cube = _grid()
    areas = [{"rank": 1, "row": 2, "col": 2, "cells": [(2, 2), (2, 3)], "lat": 6.5,
              "lon": 3.4, "date_before": "2023-02-17", "date_after": "2023-03-01",
              "drop_db": 6.0, "avg_drop_db": 5.0, "area_ha": 2.0, "n_cells": 2,
              "passes_after": 8, "recent": False}]
    steps = np.full((2, 8, 8), np.nan, dtype="float32")
    steps[:, 2, 2:4] = 6.0
    kw = dict(min_change=2.5, track_drops=steps, cache_dir=tmp_path, show_verdicts=True,
              summaries={1: validation.Summary("demolition", 2, False)})
    rows = list(csv.DictReader(io.StringIO(downloads.changes_csv(areas, **kw))))
    assert rows[0]["confidence"] == "Confirmed" and rows[0]["validators"] == "2"
    assert "all 2 tracks saw it" in rows[0]["confidence_reasons"]
    gj = json.loads(downloads.changes_geojson(cube, areas, **kw))
    assert gj["features"][0]["geometry"]["type"] in ("Polygon", "MultiPolygon")
    # Visitors who can't see verdicts get no verdict columns.
    anon = dict(kw, show_verdicts=False)
    assert "verdict" not in downloads.changes_csv(areas, **anon).splitlines()[0]
