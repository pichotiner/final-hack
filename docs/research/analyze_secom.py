"""Descriptive, reproducible SECOM audit; not a predictive model.

Usage: python3 docs/research/analyze_secom.py /tmp/opencode/secom.zip
Source: McCann & Johnston (2008), UCI, DOI 10.24432/C54305, CC BY 4.0.
Only the Python standard library is required. Input archive is never extracted.
"""

import hashlib
import json
import math
import sys
import zipfile
from collections import defaultdict
from datetime import datetime
from pathlib import Path


def analyze(path):
    with zipfile.ZipFile(path) as archive:
        data = archive.read("secom.data")
        labels = archive.read("secom_labels.data")
    rows = [[float(x) for x in line.split()] for line in data.decode().splitlines()]
    outcomes = []
    for line in labels.decode().splitlines():
        label, timestamp = line.split(maxsplit=1)
        # Source does not specify a timezone; do not invent UTC for these test times.
        test_time = datetime.strptime(timestamp.strip('"'), "%d/%m/%Y %H:%M:%S")  # noqa: DTZ007
        outcomes.append((int(label), test_time))
    assert len(rows) == len(outcomes) and rows
    width = len(rows[0])
    assert all(len(row) == width for row in rows)
    assert {label for label, _ in outcomes} <= {-1, 1}
    assert all(math.isfinite(x) or math.isnan(x) for row in rows for x in row)
    groups = defaultdict(lambda: {"rows": 0, "fails": 0})
    for label, timestamp in outcomes:
        group = groups[timestamp.strftime("%Y-%m")]
        group["rows"] += 1
        group["fails"] += label == 1
    for group in groups.values():
        group["fail_percent"] = round(100 * group["fails"] / group["rows"], 4)
    missing = sum(math.isnan(x) for row in rows for x in row)
    constant = []
    all_missing = []
    for index, column in enumerate(zip(*rows)):
        values = {x for x in column if not math.isnan(x)}
        if not values:
            all_missing.append(index)
        elif len(values) == 1:
            constant.append(index)
    fails = sum(label == 1 for label, _ in outcomes)
    return {
        "source": "https://doi.org/10.24432/C54305",
        "archive_sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest(),
        "data_sha256": hashlib.sha256(data).hexdigest(),
        "labels_sha256": hashlib.sha256(labels).hexdigest(),
        "rows": len(rows),
        "numeric_columns_in_file": width,
        "fails": fails,
        "passes": len(rows) - fails,
        "fail_percent": round(100 * fails / len(rows), 4),
        "always_pass_accuracy_percent": round(100 * (len(rows) - fails) / len(rows), 4),
        "missing_cells": missing,
        "missing_percent": round(100 * missing / (len(rows) * width), 4),
        "rows_with_missing": sum(any(math.isnan(x) for x in row) for row in rows),
        "constant_columns_ignoring_nan": len(constant),
        "all_missing_columns": len(all_missing),
        "first_test": min(t for _, t in outcomes).isoformat(),
        "last_test": max(t for _, t in outcomes).isoformat(),
        "by_test_month": dict(sorted(groups.items())),
        "stage_ranking": "NOT_IDENTIFIABLE: no named stages or exposure denominators",
        "prediction_lead_time": "NOT_IDENTIFIABLE: no per-feature availability timestamps",
    }


if __name__ == "__main__":
    print(json.dumps(analyze(sys.argv[1]), indent=2, ensure_ascii=False))
