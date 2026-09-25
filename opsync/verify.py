from __future__ import unicode_literals

import io
import json
import os

from . import diff

ERROR_VALUES = ("#REF!", "#N/A", "#VALUE!", "#DIV/0!", "#ERROR!", "#NAME?",
                "#NUM!", "#NULL!")

class Result(object):
    def __init__(self):
        self.lines = []
        self.problems = []

    def say(self, text):
        self.lines.append(text)

    def problem(self, text):
        self.problems.append(text)
        self.lines.append("PROBLEM: " + text)

def diff_against_snapshot(cfg, sheets_client, snapshot_path, store, result):

    if not os.path.exists(snapshot_path):
        result.say("no pre-write snapshot, skipping the collateral damage check")
        return

    with io.open(snapshot_path, "r", encoding="utf-8-sig") as fh:
        snap = json.load(fh)

    logged = set()
    for row in store.db.execute(
            "SELECT tab, row, header, value FROM write_log").fetchall():
        logged.add((row["tab"], row["row"], row["header"]))

    result.say("cells recorded in write_log: %d" % len(logged))

    for tab_cfg in cfg.tabs:
        before = snap.get("tabs", {}).get(tab_cfg.name)
        if not before:
            continue
        live = sheets_client.read_tab(cfg.spreadsheet_id, tab_cfg.name,
                                      cfg.header_row, cfg.first_data_row)
        old_rows = before["values"]
        expected = unexpected = by_hand = 0
        hand_examples = []
        examples = []

        for r in range(cfg.first_data_row - 1, len(live.values)):
            new_row = live.values[r]
            old_row = old_rows[r] if r < len(old_rows) else []
            width = max(len(new_row), len(old_row))
            for c in range(width):
                a = (old_row[c] if c < len(old_row) else "") or ""
                b = (new_row[c] if c < len(new_row) else "") or ""
                if a.strip() == b.strip():
                    continue
                header = live.headers[c] if c < len(live.headers) else "col %d" % (c + 1)
                key_variants = {(tab_cfg.name, r + 1, header)}

                if " / " in header:
                    key_variants.add((tab_cfg.name, r + 1, header.split(" / ")[-1]))
                if key_variants & logged:
                    expected += 1
                elif cfg.ownership_of(header) == diff.HUMAN:

                    by_hand += 1
                    if len(hand_examples) < 5:
                        hand_examples.append("    row %-5d %-24s %r -> %r"
                                             % (r + 1, header, a.strip()[:28],
                                                b.strip()[:28]))
                else:
                    unexpected += 1
                    if len(examples) < 8:
                        examples.append("    row %-5d %-24s %r -> %r"
                                        % (r + 1, header, a.strip()[:28], b.strip()[:28]))

        result.say("%s: %d cells changed since the snapshot, %d expected, "
                   "%d by hand in human columns, %d unaccounted for"
                   % (tab_cfg.name, expected + unexpected + by_hand,
                      expected, by_hand, unexpected))
        for e in hand_examples:
            result.say(e)
        for e in examples:
            result.say(e)
        if unexpected:
            result.problem("%s has %d changes this tool did not record. Either a "
                           "person edited the sheet, or something wrote without "
                           "logging." % (tab_cfg.name, unexpected))

def coverage(cfg, api, store, result):

    tracked = set()
    for tab in cfg.tabs:
        tracked.update(str(c) for c in tab.campaign_ids)

    for camp in api.campaigns():
        cid = str(camp.get("id"))
        if cid not in tracked:
            continue
        stats = camp.get("progressStats") or {}
        api_total = stats.get("totalUsers") or 0

        exposed = 0
        for _ in api.leads_in_campaign(int(cid)):
            exposed += 1

        row = store.db.execute(
            "SELECT COUNT(*) n FROM lead_campaign lc JOIN campaigns c "
            "ON c.id = lc.campaign_id WHERE c.client_id=? AND "
            "c.heyreach_campaign_id=?", (cfg.id, cid)).fetchone()
        stored = row["n"]

        queued = max(0, api_total - exposed)
        queued_note = ("  (%d more queued, not yet actioned)" % queued) if queued else ""

        result.say("campaign %-8s %-24s endpoint %-5d stored %-5d%s"
                   % (cid, (camp.get("name") or "")[:24], exposed, stored, queued_note))

        if stored < exposed:
            result.problem(
                "campaign %s: the API returns %d leads, the database has %d. "
                "%d are being lost." % (cid, exposed, stored, exposed - stored))
        elif stored > exposed:
            result.say("    note: the database holds %d more than the endpoint "
                       "returns, which happens when a lead is removed from a "
                       "campaign in HeyReach after we stored it" % (stored - exposed))

def provenance(cfg, sheets_client, store, result, sample=6):

    from . import identity

    rows = store.db.execute(
        "SELECT w.tab, w.row, w.header, w.value FROM write_log w "
        "JOIN sync_runs s ON s.id = w.run_id "
        "WHERE w.header LIKE '%Type%' AND s.client_id = ? "
        "AND w.spreadsheet_id = ? ORDER BY w.id DESC LIMIT ?",
        (cfg.id, cfg.spreadsheet_id, sample)).fetchall()
    if not rows:
        result.say("no label cells in the write log yet, nothing to trace")
        return

    cache = {}
    for w in rows:
        if w["tab"] not in cache:
            tab = sheets_client.read_tab(cfg.spreadsheet_id, w["tab"],
                                         cfg.header_row, cfg.first_data_row)
            cache[w["tab"]] = dict(tab.rows())
        live = cache[w["tab"]].get(w["row"], {})
        url = live.get(cfg.header("linkedin_url"))
        who = live.get(cfg.header("full_name"))

        lead_id, how = store.find_lead(cfg.id, url, who,
                                       live.get(cfg.header("company")))
        if lead_id is None:
            result.problem("wrote %r into %s row %s but that row's lead (%s) is "
                           "not in the database" % (w["value"], w["tab"], w["row"], who))
            continue

        msg = store.db.execute(
            "SELECT sent_at, step_label, label_detail, substr(body, 1, 70) AS snippet "
            "FROM messages WHERE lead_id = ? AND direction = 'out' AND "
            "step_label = ? ORDER BY sent_at LIMIT 1", (lead_id, w["value"])).fetchone()

        if not msg:
            result.problem(
                "wrote %r into %s row %s (%s) but that person has no stored "
                "message with that label" % (w["value"], w["tab"], w["row"], who))
            continue

        current = live.get(w["header"])
        agrees = "" if (current or "").strip() == str(w["value"]).strip() else                  "  <-- the cell no longer holds what was written"
        result.say("  %s row %-5s %-14s = %-8s  %s%s"
                   % (w["tab"], w["row"], w["header"], w["value"], who, agrees))
        result.say("      %s sent %s, matched %s"
                   % (w["value"], (msg["sent_at"] or "")[:16], msg["label_detail"]))
        result.say("      \"%s...\"" % " ".join((msg["snippet"] or "").split())[:62])

def acceptance_gaps(cfg, sheets_client, store, result):

    impossible = store.db.execute(
        "SELECT COUNT(DISTINCT lc.lead_id) n FROM lead_campaign lc "
        "JOIN campaigns c ON c.id = lc.campaign_id "
        "JOIN messages m ON m.lead_id = lc.lead_id AND m.direction = 'out' "
        "WHERE c.client_id = ? AND lc.accepted_at IS NULL",
        (cfg.id,)).fetchone()["n"]

    from . import identity as identity_mod

    for tab_cfg in cfg.tabs:
        tab = sheets_client.read_tab(cfg.spreadsheet_id, tab_cfg.name,
                                     cfg.header_row, cfg.first_data_row)
        header = cfg.header("invite_accepted")

        index = identity_mod.Index()
        for row_number, record in tab.rows():
            index.add(row_number,
                      record.get(cfg.header("linkedin_url")),
                      record.get(cfg.header("full_name")),
                      record.get(cfg.header("company")),
                      record.get(cfg.header("title")),
                      record.get(cfg.header("location")))
        by_row = dict(tab.rows())
        ours = set()
        for lead in store.leads_for_tab(cfg.id, tab_cfg.name):
            row, _how = index.find(lead["linkedin_url"], lead["full_name"],
                                   lead["company"])
            if row is not None:
                ours.add(row)

        filled = sum(1 for row in ours
                     if (by_row[row].get(header) or "").strip())
        total_filled = sum(1 for _, r in tab.rows()
                           if (r.get(header) or "").strip())
        known = store.db.execute(
            "SELECT COUNT(DISTINCT lc.lead_id) n FROM lead_campaign lc "
            "JOIN campaigns c ON c.id = lc.campaign_id "
            "WHERE c.client_id = ? AND c.persona_tab = ? "
            "AND lc.status = 'ConnectionAccepted'",
            (cfg.id, tab_cfg.name)).fetchone()["n"]
        result.say("%-20s of the %d rows this workspace knows, %d carry an "
                   "acceptance date; HeyReach reports %d accepted, short by %d"
                   % (tab_cfg.name, len(ours), filled, known, known - filled))
        if total_filled > filled:
            result.say("    a further %d rows on this tab carry one and belong to "
                       "campaigns this workspace no longer holds, so they are "
                       "outside what can be checked"
                       % (total_filled - filled))
        if known - filled > 0:
            result.say("    no date exists for those anywhere: the API carries none "
                       "and the webhook never recorded one")

    result.say("%d of this client's leads were messaged with no acceptance date on "
               "record, which cannot have happened in reality and measures how much "
               "the webhook stream is dropping" % impossible)

def all_tab_names(cfg, sheets_client):

    meta = sheets_client.api.spreadsheets().get(
        spreadsheetId=cfg.spreadsheet_id, includeGridData=False).execute()
    return [s["properties"]["title"] for s in meta["sheets"]
            if s["properties"].get("sheetType", "GRID") == "GRID"]

def formula_health(cfg, sheets_client, tab_names, result):
    bad = 0
    for name in tab_names:
        try:
            tab = sheets_client.read_tab(cfg.spreadsheet_id, name, 1, 2)
        except Exception as err:
            result.say("%-26s could not read: %s" % (name, str(err)[:60]))
            continue
        count = 0
        for row in tab.values:
            for cell in row:
                if cell and str(cell).strip() in ERROR_VALUES:
                    count += 1
        bad += count
        result.say("%-26s %s" % (name, "clean" if count == 0 else "%d ERROR CELLS" % count))
    if bad:
        result.problem("%d error cells across the workbook" % bad)

def panel_rows(cfg, store):

    import time

    runs = store.db.execute(
        "SELECT started_at, finished_at, mode, status, cells_written, disagreements "
        "FROM sync_runs WHERE client_id=? ORDER BY id DESC LIMIT 25",
        (cfg.id,)).fetchall()
    last = runs[0] if runs else None
    day = [r for r in runs if (r["started_at"] or "")[:10] == time.strftime("%Y-%m-%d")]
    failures = [r for r in day if r["status"] not in ("ok", None)]

    labels = store.label_source_counts(cfg.id)
    total = sum(labels.values()) or 1
    unlabelled = store.db.execute(
        "SELECT COUNT(*) n FROM messages WHERE client_id=? AND direction='out' "
        "AND step_label IS NULL", (cfg.id,)).fetchone()["n"]
    written_total = store.db.execute(
        "SELECT COUNT(*) n FROM write_log").fetchone()["n"]
    watched = store.db.execute(
        "SELECT COUNT(*) n FROM lead_campaign WHERE observed_accepted_at IS NOT NULL "
        "OR observed_sent_at IS NOT NULL").fetchone()["n"]

    rows = [
        ["opsync status", "", "", "", "", ""],
        ["This tab is written by the automation. Nothing reads it and no formula "
         "depends on it. Hidden on purpose.", "", "", "", "", ""],
        ["", "", "", "", "", ""],
        ["Last run", (last["started_at"] if last else "never"),
         (last["status"] if last else ""), "", "", ""],
        ["Cells written on that run", (last["cells_written"] if last else 0), "", "", "", ""],
        ["Differences needing a human", (last["disagreements"] if last else 0), "", "", "", ""],
        ["Runs today", len(day), "", "", "", ""],
        ["Failed or refused today", len(failures),
         ("nothing to do" if not failures else "LOOK AT THE LOG"), "", "", ""],
        ["", "", "", "", "", ""],
        ["Cells written since the start", written_total, "", "", "", ""],
        ["Messages matched to one Drips template", labels.get("drips", 0),
         "%.0f%%" % (100.0 * labels.get("drips", 0) / total), "", "", ""],
        ["  of those, decided between two near-identical ones",
         labels.get("closest", 0),
         ("" if not labels.get("closest", 0)
          else "the higher score was taken; separate the copy on Drips to "
               "remove the doubt"), "", "", ""],
        ["Messages left unlabelled", unlabelled,
         "the sender's own replies, and conversations outside any campaign", "", "", ""],
        ["Dates obtained by watching, not by webhook", watched, "", "", "", ""],
        ["", "", "", "", "", ""],
        ["What it will never do", "", "", "", "", ""],
        ["", "overwrite anything a person typed", "", "", "", ""],
        ["", "write outside the LI columns and the invite dates", "", "", "", ""],
        ["", "write more than %d cells in one run" % cfg.max_cells_per_run, "", "", "", ""],
        ["", "touch a formula tab", "", "", "", ""],
        ["", "", "", "", "", ""],
        ["Written at", time.strftime("%Y-%m-%d %H:%M"), "", "", "", ""],
    ]
    return rows
