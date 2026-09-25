from __future__ import unicode_literals

import io
import json
import os

CONTROL_FILE = "control.json"

def _path(base):
    return os.path.join(base or ".", CONTROL_FILE)

def read(base):
    try:
        with io.open(_path(base), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (IOError, OSError, ValueError):
        return {"paused": {}}
    if not isinstance(data, dict):
        return {"paused": {}}
    paused = data.get("paused")
    if not isinstance(paused, dict):
        paused = {}
    return {"paused": paused}

def _write(base, data):
    path = _path(base)
    directory = os.path.dirname(path)
    if directory and not os.path.isdir(directory):
        os.makedirs(directory)
    tmp = path + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(data, indent=2, sort_keys=True))
    if os.path.exists(path):
        os.remove(path)
    os.rename(tmp, path)

def is_paused(base, client_id):
    return client_id in read(base)["paused"]

def pause_reason(base, client_id):
    return read(base)["paused"].get(client_id)

def pause(base, client_id, who, reason=""):

    import time
    data = read(base)
    data["paused"][client_id] = {
        "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "by": who or "unknown",
        "reason": reason or "",
    }
    _write(base, data)
    return data["paused"][client_id]

def resume(base, client_id):
    data = read(base)
    data["paused"].pop(client_id, None)
    _write(base, data)

def rollback_cells(cells, tab):

    to_clear, left = [], []
    for cell in cells:
        if not tab.has(cell["header"]):
            left.append((cell, "column not found"))
            continue
        col = tab.column_index(cell["header"])
        index = cell["row"] - 1
        row = tab.values[index] if 0 <= index < len(tab.values) else []
        now = (row[col] if col < len(row) else "") or ""
        wrote = cell["value"] or ""
        if not now.strip():
            left.append((cell, "already empty"))
        elif now.strip() == wrote.strip():
            to_clear.append((cell["row"], col))
        else:
            left.append((cell, "changed since by a person"))
    return to_clear, left

def rollback_plan(st, run_id):

    rows = st.db.execute(
        "SELECT id, run_id, spreadsheet_id, tab, row, header, value, reason "
        "FROM write_log WHERE run_id=? ORDER BY tab, row, header",
        (run_id,)).fetchall()
    cells, created = [], []
    for r in rows:
        item = {
            "id": r["id"], "spreadsheet_id": r["spreadsheet_id"],
            "tab": r["tab"], "row": r["row"], "header": r["header"],
            "value": r["value"], "reason": r["reason"],
        }
        cells.append(item)
        if r["reason"] in ("new row", "insert"):
            created.append(item)
    tabs = sorted(set(c["tab"] for c in cells))
    return {
        "run_id": run_id,
        "cells": cells,
        "cells_to_clear": len(cells),
        "rows_the_tool_created": len(set((c["tab"], c["row"]) for c in created)),
        "tabs": tabs,
    }
