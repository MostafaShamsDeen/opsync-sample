from __future__ import unicode_literals, print_function

import argparse
import io
import json
import os
import time
import sys

from . import config as config_mod
from . import diff, projection, store as store_mod

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEFAULT_CONFIG_DIR = os.path.join(ROOT, "config", "clients")

DEFAULT_DB = os.path.join(ROOT, "data")

PANEL_TAB = "_opsync"

def _now_stamp():

    import datetime
    return datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

def _client_store(args, client_id):

    return store_mod.Store(store_mod.path_for(client_id, args.db))

def _skip_if_paused(args, client_id):

    from . import control
    if not control_mod_paused(args, client_id):
        return False
    info = control.pause_reason(args.db, client_id) or {}
    print("client %s" % client_id)
    print("  PAUSED since %s by %s%s. Skipped, not failed."
          % (info.get("at", "?"), info.get("by", "?"),
             (": " + info["reason"]) if info.get("reason") else ""))
    return True

def control_mod_paused(args, client_id):
    from . import control
    return control.is_paused(args.db, client_id)

def _clients_with_store(args):

    for cid, cfg in sorted(_configs(args).items()):
        st = _client_store(args, cid)
        try:
            yield cid, cfg, st
        finally:
            st.close()

def _configs(args):
    configs = config_mod.load_all(args.config_dir)
    if not configs:
        raise SystemExit("no client configs found in %s" % args.config_dir)
    if args.client:
        if args.client not in configs:
            raise SystemExit("unknown client %r. Known: %s"
                             % (args.client, ", ".join(sorted(configs))))
        return {args.client: configs[args.client]}
    return configs

def _api_key(cfg):

    key = os.environ.get(cfg.api_key_env)
    if not key:
        raise SystemExit(
            "%s is not set.\n"
            "HeyReach issues one key per workspace. Copy the key for %s from "
            "Settings, Integrations, API Integration, and set it for this "
            "session only:\n"
            "  set %s=...        (cmd)\n"
            "  $env:%s='...'     (PowerShell)\n"
            "  export %s='...'   (bash)\n"
            "It is a credential. It does not go in this repository, which sits "
            "inside the junction to the Obsidian vault."
            % (cfg.api_key_env, cfg.name, cfg.api_key_env, cfg.api_key_env,
               cfg.api_key_env))
    return key

def cmd_doctor(args):
    print("python           %s" % sys.version.split()[0])
    print("config directory %s" % args.config_dir)
    configs = config_mod.load_all(args.config_dir)
    for cid, cfg in sorted(configs.items()):
        print("  client %-10s sheet %s, %d tab(s), cap %d cells/run"
              % (cid, cfg.spreadsheet_id, len(cfg.tabs), cfg.max_cells_per_run))
        for tab in cfg.tabs:
            print("    tab %-22s campaigns %s" % (tab.name, ", ".join(tab.campaign_ids)))
        machine = [h for h, o in cfg.ownership.items() if o == diff.MACHINE]
        shared = [h for h, o in cfg.ownership.items() if o == diff.SHARED]
        print("    machine-owned columns: %d, shared: %d, everything else is human"
              % (len(machine), len(shared)))
    print("databases        one per client, under %s"
          % os.path.join(args.db, store_mod.CLIENT_DB_DIR))
    for cid in sorted(configs):
        path = store_mod.path_for(cid, args.db)
        exists = "ok" if os.path.exists(path) else "not created yet"
        st = store_mod.Store(path)
        st.close()
        print("  %-14s %s  (%s)" % (cid, path, exists))
    print("                 schema ok")
    for cid, cfg in sorted(configs.items()):
        print("%-24s %s" % (cfg.api_key_env, "set" if os.environ.get(cfg.api_key_env) else "NOT SET"))
    try:
        from google.oauth2 import service_account
        print("google libs      installed")
    except ImportError:
        print("google libs      not installed (only needed to read or write sheets)")
    return 0

def cmd_discover(args):
    from . import heyreach
    for cid, cfg, st in _clients_with_store(args):
        client = heyreach.Client(_api_key(cfg), store=st, client_id=cid)
        client.check_key()
        print("API key authenticates for %s" % cid)
        campaign = cfg.tabs[0].campaign_ids[0] if cfg.tabs else None
        report = client.discover(campaign_id=campaign)
        print(json.dumps(report, indent=2, default=str)[:8000])
    st.close()
    return 0

def _templates(cfg, args):

    from . import labeller

    if args.credentials:
        from . import sheets
        client = sheets.SheetsClient(args.credentials, read_only=True)
        tab = client.read_tab(cfg.spreadsheet_id, cfg.drips_tab, 1, 2)
        grid = tab.values
        source = "Drips tab, live"
    else:
        path = os.path.join(ROOT, "data", "snapshots", "%s-drips.json" % cfg.id)
        if not os.path.exists(path):
            raise SystemExit(
                "no Google credentials and no Drips snapshot at %s.\n"
                "Without the campaign copy every message is unlabelled." % path)
        with io.open(path, "r", encoding="utf-8-sig") as fh:
            snap = json.load(fh)
        grid = _grid_from_snapshot(snap["templates"])
        source = "Drips snapshot, %s" % snap.get("source", "undated")

    templates = labeller.parse_drips(grid)
    by_persona = {}
    for t in templates:
        by_persona.setdefault(t.persona, []).append(t)
    return by_persona, source

def _grid_from_snapshot(items):

    blocks = {}
    for t in items:
        blocks.setdefault(t["persona"], []).append(t)
    grid = []

    def put(r, c, value):
        while len(grid) <= r:
            grid.append([])
        while len(grid[r]) <= c:
            grid[r].append("")
        grid[r][c] = value

    for index, persona in enumerate(sorted(blocks)):
        col = 1 + index * 6
        put(1, col, persona)
        row = 2
        bands = sorted(set(t["band"] for t in blocks[persona]))
        for band in bands:
            put(row, col, "LI %d" % band)
            row += 1
            for variant in ("A", "B"):
                put(row, col, "Type %s" % variant)
                row += 1
                hit = [t for t in blocks[persona]
                       if t["band"] == band and t["variant"] == variant]
                put(row, col, hit[0]["text"] if hit else "")
                row += 1
    return grid

def _report_gap(st, cid):

    import datetime
    row = st.db.execute(
        "SELECT started_at FROM sync_runs WHERE status = 'ok' "
        "ORDER BY id DESC LIMIT 1").fetchone()
    if not row or not row[0]:
        return
    try:
        last = datetime.datetime.strptime(row[0][:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return
    gap = (datetime.datetime.now() - last).total_seconds() / 3600.0
    if gap < 2:
        return
    print("  last ran           %.1f hours ago, at %s" % (gap, row[0][:16]))
    print("                     catching up. Cells are not lost, but an "
          "acceptance seen now may have happened earlier in that gap")
    if last.date() != datetime.datetime.now().date():
        print("                     THE GAP CROSSED MIDNIGHT, so an acceptance "
              "dated today may belong to %s" % last.date().isoformat())

def cmd_fetch(args):
    from . import heyreach, pipeline
    for cid, cfg, st in _clients_with_store(args):
        if _skip_if_paused(args, cid):
            continue
        _report_gap(st, cid)
        api = heyreach.Client(_api_key(cfg), store=st, client_id=cid)
        by_persona, source = _templates(cfg, args)
        print("client %s" % cid)
        print("templates          %d from %s"
              % (sum(len(v) for v in by_persona.values()), source))
        report = pipeline.fetch_client(cfg, api, st, by_persona)
        for line in report.lines():
            print(line)

        from . import staging
        stg = cfg.staging
        records = None
        if stg and cfg.staging_retired_on:
            print("staging            RETIRED on %s. Connection dates now come "
                  "from the API and this tool's own observation only"
                  % cfg.staging_retired_on)
            stg = None
        if args.credentials and stg:
            from . import sheets
            sc = sheets.SheetsClient(args.credentials, read_only=True)
            records = {}
            for key, tab_name in (("invite_sent", stg["invite_sent_tab"]),
                                  ("accepted", stg["accepted_tab"])):
                tab = sc.read_tab(stg["spreadsheet_id"], tab_name, 1, 2)
                records[key] = [rec for _, rec in tab.rows()]
            print("staging            live, %s" % stg["spreadsheet_id"][:12] + "..")

            try:
                snap_dir = os.path.join(ROOT, "data", "snapshots")
                if not os.path.isdir(snap_dir):
                    os.makedirs(snap_dir)
                cols = sorted(set(k for rs in records.values()
                                  for r in rs for k in r))
                snap = {"source": "read live %s" % _now_stamp(),
                        "columns": cols, "tabs": {}}
                for key, tab_name in (("invite_sent", stg["invite_sent_tab"]),
                                      ("accepted", stg["accepted_tab"])):
                    snap["tabs"][tab_name] = {"values": [cols] + [
                        [r.get(c, "") for c in cols] for r in records[key]]}
                with io.open(os.path.join(snap_dir, "%s-staging.json" % cid),
                             "w", encoding="utf-8") as fh:
                    fh.write(json.dumps(snap, ensure_ascii=False))
                print("staging snapshot   refreshed, %d + %d rows"
                      % (len(records["invite_sent"]), len(records["accepted"])))
            except Exception as err:
                print("staging snapshot   not refreshed: %s" % str(err)[:90])
        else:
            path = os.path.join(ROOT, "data", "snapshots", "%s-staging.json" % cid)
            if os.path.exists(path):
                with io.open(path, "r", encoding="utf-8-sig") as fh:
                    snap = json.load(fh)
                cols = snap["columns"]
                records = {}
                for key, tab_name in (("invite_sent", "Connection_Request_Sent"),
                                      ("accepted", "Accepted_Leads_Import")):
                    rows = snap["tabs"][tab_name]["values"][1:]
                    records[key] = [dict(zip(cols, r)) for r in rows]
                print("staging            snapshot, %s" % snap.get("source", "undated"))
        if records:
            for line in staging.ingest(cfg, st, records).lines():
                print(line)
        else:
            print("staging            unavailable, invite and accepted dates "
                  "will stay blank")

        rep = pipeline.relabel(cfg, st, by_persona,
                               only_unresolved=not getattr(args, "relabel_all", False))
        for line in rep.lines():
            print(line)
    st.close()
    return 0

def _persona_resolver(st):

    cache = {}

    def resolve(lead_id):
        if lead_id not in cache:
            names = [r["persona_tab"] for r in st.campaigns_of_lead(lead_id)
                     if r["persona_tab"]]
            cache[lead_id] = names[0] if names else ""
        return cache[lead_id]
    return resolve

class _Queue(object):

    def __init__(self, fresh, parsed, layout, decided, problems, held_back=0,
                 no_persona=0, no_contact=0):
        self.fresh = fresh
        self.parsed = parsed
        self.layout = layout
        self.decided = decided
        self.problems = problems

        self.held_back = held_back
        self.no_persona = no_persona
        self.no_contact = no_contact

def _queue_state(cfg, st, sheets_client, may_write=False):

    from . import replies
    everything = replies.build(st, cfg, _persona_resolver(st))
    fresh = replies.actionable(everything)
    held = len(everything) - len(fresh)
    blocked = replies.held_back(everything)
    no_persona = len([r for r in blocked
                      if not str(r.get("persona") or "").strip()])
    no_contact = len(blocked) - no_persona
    if not hasattr(sheets_client, "read_grid"):
        return _Queue(fresh, {}, {}, {}, [], held, no_persona, no_contact)

    grid = sheets_client.read_grid(cfg.spreadsheet_id, replies.QUEUE_TAB)
    if not grid:
        return _Queue(fresh, {}, {}, {}, [], held, no_persona, no_contact)

    adds = replies.missing_header_writes(grid)
    if adds and may_write:

        sheets_client.ensure_grid_size(
            cfg.spreadsheet_id, replies.QUEUE_TAB,
            columns=max(c for _, c, _ in adds) + 1)
        sheets_client.update_scattered(cfg.spreadsheet_id, replies.QUEUE_TAB, adds)
        print("  queue: added column(s) %s to an existing tab"
              % ", ".join(h for _, _, h in adds))
        grid = sheets_client.read_grid(cfg.spreadsheet_id, replies.QUEUE_TAB)

    parsed, layout, problems = replies.parse_grid(grid)
    return _Queue(fresh, parsed, layout, replies.decisions(parsed), problems,
                  held, no_persona, no_contact)

def _drop_overflowing(cfg, client, queue, reply_cells):

    from . import replies as replies_mod, responses
    if not reply_cells:
        return reply_cells, {}
    blocks = _report_blocks(cfg, client)
    if not blocks:
        return reply_cells, {}

    by_lead = dict((r["lead_id"], r) for r in queue.fresh)
    wanted = {}
    for lead_id in reply_cells:
        row = by_lead.get(lead_id)
        if row is None:
            continue
        sentiment = queue.decided.get(row["lead_key"], "")
        wanted.setdefault(sentiment, []).append(lead_id)

    kept = dict(reply_cells)
    blocked = {}
    for sentiment, lead_ids in wanted.items():
        fits, block = responses.room_for(blocks, sentiment, len(lead_ids))
        if fits:
            continue
        for lead_id in lead_ids:
            kept.pop(lead_id, None)
            blocked[lead_id] = (
                "%s is full at %d of %d on the report tab. Run "
                "`headroom --write` to make room"
                % (block.heading, block.used, block.capacity))
    return kept, blocked

def _reply_cells(cfg, fresh, decided):

    from . import replies
    by_key = dict((r["lead_key"], r) for r in fresh)
    out = {}
    for key, sentiment in decided.items():
        row = by_key.get(key)
        if row is None:
            continue
        cells = replies.reply_for(cfg, row, sentiment)
        if cells:
            out[row["lead_id"]] = cells
    return out

def _plan_for_client(cfg, st, sheets_client, reply_cells=None,
                     queue_rows=None):

    from . import replies as replies_mod
    reply_cells = reply_cells or {}
    queue_rows = queue_rows or {}
    plans = []
    for tab_cfg in cfg.tabs:
        tab = sheets_client.read_tab(cfg.spreadsheet_id, tab_cfg.name,
                                     cfg.header_row, cfg.first_data_row)
        required = [cfg.header("full_name"), cfg.header("linkedin_url")]
        tab.assert_headers(required)

        from .identity import Index
        index = Index()
        for row_number, record in tab.rows():
            index.add(row_number,
                      record.get(cfg.header("linkedin_url")),
                      record.get(cfg.header("full_name")),
                      record.get(cfg.header("company")),
                      record.get(cfg.header("title")),
                      record.get(cfg.header("location")))

        current_by_row = dict(tab.rows())
        plan = diff.Plan(cfg.id, cfg.spreadsheet_id, tab_cfg.name)
        ownership = projection.ownership_map(cfg, sorted(tab.names))
        info = {}

        from . import newrows
        all_leads = list(st.leads_for_tab(cfg.id, tab_cfg.name))
        row_plan = newrows.plan(
            cfg, tab_cfg, tab, all_leads, index,
            lambda lead: projection.project_lead(
                cfg, lead, st.messages_for_lead(lead["id"]),
                reply_cells.get(lead["id"])))

        for lead in all_leads:
            row, how = index.find(lead["linkedin_url"], lead["full_name"],
                                  lead["company"])
            reply = reply_cells.get(lead["id"])
            if row is None:

                info[lead["id"]] = {"row": None, "reply": None}
                continue
            desired = projection.project_lead(
                cfg, lead, st.messages_for_lead(lead["id"]), reply)
            desired = dict((h, v) for h, v in desired.items() if tab.has(h))
            current = current_by_row.get(row, {})
            projection.apply_mirrors(cfg, tab, current, desired)
            changes = diff.compare_row(row, current, desired, ownership)
            plan.add(changes)

            entry = replies_mod.recorded_entry(cfg, current_by_row.get(row, {}))
            if entry:
                entry.compare(queue_rows.get(lead["id"]) or {})

            outcome = None
            if reply:

                want = [h for h, v in reply.items() if str(v).strip()]
                seen = [c.outcome for c in changes if c.header in want]
                if diff.FILL in seen:
                    outcome = "fill"
                elif diff.AGREE in seen:
                    outcome = "agree"
                elif seen:
                    outcome = "other"
                else:
                    outcome = "nothing"
            info[lead["id"]] = {
                "row": row, "reply": outcome,
                "recorded": entry,
            }
        plans.append((tab, plan, info, row_plan))
    return plans

def _sheets_for(args, cfg, write):

    from . import sheets
    if args.credentials:
        return sheets.SheetsClient(args.credentials, read_only=not write), "live sheet"
    if write:
        raise SystemExit(
            "--write needs Google credentials. Set OPSYNC_CREDENTIALS or pass "
            "--credentials. Planning works offline from a snapshot; writing "
            "does not, on purpose.")
    path = os.path.join(ROOT, "data", "snapshots", "%s-sheet.json" % cfg.id)
    if not os.path.exists(path):
        raise SystemExit("no Google credentials and no sheet snapshot at %s" % path)
    client = sheets.OfflineSheets(path)
    return client, client.source

def _report_blocks(cfg, client):

    from . import responses
    tab = getattr(cfg, "report_tab", "") or ""
    if not tab or not hasattr(client, "read_grid"):
        return None
    grid = client.read_grid(cfg.spreadsheet_id, tab)
    if not grid:
        return None
    return responses.map_blocks(grid, total_rows=client.row_count(
        cfg.spreadsheet_id, tab) or len(grid))

def cmd_headroom(args):

    from . import responses
    configs = _configs(args)
    exit_code = 0
    for cid, cfg in sorted(configs.items()):
        if not args.credentials:
            raise SystemExit("headroom needs Google credentials")
        client, _ = _sheets_for(args, cfg, write=bool(args.write))
        blocks = _report_blocks(cfg, client)
        if blocks is None:
            print("%-10s no report tab configured, nothing to measure" % cid)
            continue

        print("%s / %s" % (cid, cfg.report_tab))
        for row in responses.summary_rows(blocks):
            print("  %-20s %-10s %-12s %s" % tuple(row))

        plan = responses.growth_plan(blocks)
        if not plan:
            print("  every group has room")
            if args.write:
                _renumber(cfg, client, blocks)
            continue
        problems = responses.check(plan)
        if problems:
            for problem in problems:
                print("  REFUSED: %s" % problem)
            exit_code = 2
            continue
        for step in plan:
            print("  %s %d rows before row %d because %s"
                  % ("inserting" if args.write else "would insert",
                     step["rows"], step["before_row"], step["why"]))
        if not args.write:
            print("  dry run, nothing inserted. Add --write to make the room.")
            continue

        sheet_id = client.sheet_id_of(cfg.spreadsheet_id, cfg.report_tab)
        st = _client_store(args, cid)
        run_id = st.start_run(cid, "headroom")
        added = 0

        for step in sorted(plan, key=lambda s: s["before_row"], reverse=True):
            client.insert_rows(cfg.spreadsheet_id, sheet_id,
                               step["before_row"], step["rows"])
            st.log_write(run_id, cfg.spreadsheet_id, cfg.report_tab,
                         step["before_row"], "insert rows",
                         "%d rows: %s" % (step["rows"], step["why"]), "insert")
            added += step["rows"]
        st.finish_run(run_id, "ok", added, 0)
        st.close()
        print("  inserted %d rows" % added)

        after = _report_blocks(cfg, client)
        for row in responses.summary_rows(after):
            print("  now %-20s %-10s %-12s %s" % tuple(row))

        _renumber(cfg, client, after)
    return exit_code

def _renumber(cfg, client, blocks):

    from . import responses
    grid = client.read_grid(cfg.spreadsheet_id, cfg.report_tab) or []
    cells = responses.numbering_plan(blocks, grid)
    if not cells:
        return 0
    client.update_scattered_formulas(cfg.spreadsheet_id, cfg.report_tab, cells)
    print("  numbering: %d cells now follow the names" % len(cells))
    return len(cells)

def _queue_tab_name():
    from . import replies
    return replies.QUEUE_TAB

def _column_counts(changes):

    counts = {}
    for change in changes:
        counts[change.header] = counts.get(change.header, 0) + 1
    return sorted(counts.items(), key=lambda kv: -kv[1])[:8]

def _queue_write(cfg, client, queue, info_by_lead, refusals, wrote):

    from . import replies

    for row in queue.fresh:
        info = info_by_lead.get(row["lead_id"]) or {}
        sheet_row = info.get("row")
        row["row"] = str(sheet_row) if sheet_row else ""
        row["status"], row["done"] = replies.resolve(
            queue.decided.get(row["lead_key"], ""), row["persona"], row["row"],
            info.get("reply"), wrote, refusals.get(row["lead_id"]),
            info.get("recorded"),
            getattr(cfg, "reply_done_requires_match", False))

    open_rows, done_rows = replies.settled(queue.fresh, info_by_lead)
    if done_rows:
        print("replies            %d already recorded on the persona tab, "
              "not queued" % len(done_rows))
    queue.fresh = open_rows

    sheet_id, _created = client.ensure_tab(
        cfg.spreadsheet_id, replies.QUEUE_TAB, hidden=False,
        rows=max(200, len(queue.fresh) + 100), columns=len(replies.COLUMNS))

    layout = queue.layout
    touched = 0
    if not layout:

        touched += client.update_scattered(
            cfg.spreadsheet_id, replies.QUEUE_TAB,
            [(1, i, h) for i, h in enumerate(replies.HEADERS)])
        layout = dict((h, i) for i, h in enumerate(replies.HEADERS))
    elif queue.problems and not queue.parsed:

        return 0

    removable, kept = replies.retired(queue.fresh, queue.parsed)
    if removable:
        client.delete_rows(cfg.spreadsheet_id, sheet_id,
                           [e["_row"] for e in removable])
        touched += len(removable)

        grid = client.read_grid(cfg.spreadsheet_id, replies.QUEUE_TAB)
        parsed, layout, _problems = replies.parse_grid(grid)
    else:
        parsed = queue.parsed
    if kept:

        done_col = layout.get(replies.HEADER_OF[replies.DONE])
        marks = [(e["_row"], done_col, replies.DONE_YES) for e in kept
                 if done_col is not None
                 and str(e.get(replies.DONE) or "").strip().lower() != replies.DONE_YES]
        if marks:
            touched += client.update_scattered(
                cfg.spreadsheet_id, replies.QUEUE_TAB, marks)
        print("  queue: %d row(s) no longer in the queue kept because somebody "
              "has already set a sentiment on them%s"
              % (len(kept), (", %d marked done" % len(marks)) if marks else ""))

    updates, appends = replies.plan_grid(queue.fresh, parsed, layout)
    touched += client.update_scattered(cfg.spreadsheet_id, replies.QUEUE_TAB,
                                       updates)
    if appends:
        client.append_rows(cfg.spreadsheet_id, replies.QUEUE_TAB, appends)
        touched += len(appends) * len(replies.COLUMNS)

    on_tab = max([e["_row"] for e in parsed.values()] or [1])
    last_row = max(on_tab + len(appends), len(queue.fresh) + 1)

    client.format_queue_tab(
        cfg.spreadsheet_id, sheet_id, replies.COLUMNS,
        layout[replies.HEADER_OF[replies.SENTIMENT]], replies.CHOICES,
        last_row, layout=layout,
        done_column=layout[replies.HEADER_OF[replies.DONE]],
        done_value=replies.DONE_YES)
    return touched

def cmd_queue(args):

    from . import replies
    for cid, cfg, st in _clients_with_store(args):
        if not args.credentials:
            raise SystemExit("queue needs Google credentials")
        client, _ = _sheets_for(args, cfg, write=True)
        queue = _queue_state(cfg, st, client, may_write=True)
        for problem in queue.problems:
            print("  queue: %s" % problem)

        info_by_lead = {}
        reply_cells = _reply_cells(cfg, queue.fresh, queue.decided)
        by_lead = dict((r["lead_id"], r) for r in queue.fresh)
        for _tab, _plan, info, _rows in _plan_for_client(
                cfg, st, client, reply_cells, by_lead):
            info_by_lead.update(info)

        n = _queue_write(cfg, client, queue, info_by_lead, {}, wrote=False)
        waiting = len([r for r in queue.fresh if not r.get("done")])
        print("%-22s %d replies, %d decided, %d still need a person, "
              "%d cells touched in %s"
              % (cid, len(queue.fresh), len(queue.decided), waiting, n,
                 replies.QUEUE_TAB))
        unattributed = len([r for r in queue.fresh if not r["persona"]])
        if unattributed:
            print("  %d of them have no campaign attributed, so nothing can be "
                  "written for those rows" % unattributed)
    st.close()
    return 0

def cmd_plan(args, write=False):
    from . import sheets
    allowed = config_mod.allowlist(config_mod.load_all(args.config_dir))
    exit_code = 0

    for cid, cfg, st in _clients_with_store(args):
        if _skip_if_paused(args, cid):
            continue
        client, source = _sheets_for(args, cfg, write)
        print("reading            %s" % source)
        run_id = st.start_run(cid, "write" if write else "dry-run")
        written = disagreed = 0

        queue = _queue_state(cfg, st, client, may_write=write)
        for problem in queue.problems:
            print("  queue: %s" % problem)
        reply_cells = _reply_cells(cfg, queue.fresh, queue.decided)
        reply_cells, blocked = _drop_overflowing(cfg, client, queue, reply_cells)
        if blocked:
            print("report tab         %d repl%s held back: %s"
                  % (len(blocked), "y" if len(blocked) == 1 else "ies",
                     sorted(set(blocked.values()))[0]))
        if queue.fresh:
            print("replies            %d in the queue, %d decided, %d to apply"
                  % (len(queue.fresh), len(queue.decided), len(reply_cells)))
        info_by_lead = {}
        refusals = {}
        created = 0

        summary = {"tabs": [], "rows_created": 0,
                   "queue_rows": len(queue.fresh),
                   "queue_waiting": None,
                   "queue_held_back": queue.held_back}

        try:
            by_lead = dict((r["lead_id"], r) for r in queue.fresh)
            for tab, plan, info, row_plan in _plan_for_client(
                    cfg, st, client, reply_cells, by_lead):
                info_by_lead.update(info)
                print(plan.summary())
                counts = plan.counts()
                summary["tabs"].append({
                    "tab": tab.name, "fill": counts[diff.FILL],
                    "agree": counts[diff.AGREE], "skip": counts[diff.SKIP],
                    "to_look_at": len(plan.actionable_disagreements),
                    "reported_only": counts[diff.DISAGREE]
                                     - len(plan.actionable_disagreements),
                    "by_column": _column_counts(plan.actionable_disagreements),
                    "new_rows": len(row_plan),
                })
                for header, n in plan.by_header(plan.disagreements):
                    print("  disagree  %-24s %d" % (header, n))
                for change in plan.actionable_disagreements[:15]:
                    print("  LOOK AT   row %-5d %-22s %s"
                          % (change.row, change.header, change.note))
                disagreed += len(plan.actionable_disagreements)

                problems = plan.check(cfg.max_cells_per_run, allowed)
                if problems:
                    for p in problems:
                        print("  REFUSED: %s" % p)
                    st.finish_run(run_id, "refused", 0, disagreed, "; ".join(problems))
                    exit_code = 2

                    for lead_id in info:
                        refusals[lead_id] = problems[0]
                    continue

                if len(row_plan):
                    print(row_plan.summary())
                row_problems = row_plan.check(cfg.max_new_rows_per_run, allowed)
                if row_problems:
                    for p in row_problems:
                        print("  REFUSED (new rows): %s" % p)
                    exit_code = 2
                elif write and len(row_plan):
                    added = client.append_rows_for_leads(
                        cfg.spreadsheet_id, tab, row_plan.rows(),
                        run_id=run_id, store=st)
                    created += added
                    written += sum(len(v) for _r, v in row_plan.rows())
                    print("  added %d new row(s), from row %d"
                          % (added, row_plan.first_row))
                elif len(row_plan):
                    print("  would add %d new row(s) (dry run)" % len(row_plan))

                if write:
                    written += client.apply(cfg.spreadsheet_id, tab, plan.fills,
                                            run_id=run_id, store=st)
                    print("  wrote %d cells" % len(plan.fills))
                else:
                    print("  would write %d cells (dry run)" % len(plan.fills))
            if created:
                print("new rows            %d created this run" % created)
                summary["rows_created"] = created
            if exit_code != 2:
                st.finish_run(run_id, "ok", written, disagreed)

            if write:
                try:
                    for lead_id, why in blocked.items():
                        refusals.setdefault(lead_id, why)
                    n = _queue_write(cfg, client, queue, info_by_lead,
                                     refusals, wrote=True)
                    waiting = len([r for r in queue.fresh if not r.get("done")])
                    summary["queue_rows"] = len(queue.fresh)
                    summary["queue_waiting"] = waiting
                    summary["queue_held_back"] = queue.held_back
                    line = ("  reply queue: %d cells updated, %d rows still "
                            "need a person" % (n, waiting))
                    if queue.held_back:
                        why = []
                        if queue.no_persona:
                            why.append("%d with no persona" % queue.no_persona)
                        if queue.no_contact:
                            why.append("%d the campaign never contacted"
                                       % queue.no_contact)
                        line += (", %d reply(s) held back (%s)"
                                 % (queue.held_back, ", ".join(why)))
                    print(line)
                except Exception as err:
                    print("  reply queue not updated: %s" % str(err)[:140])

            if write:
                try:
                    from . import verify as verify_mod
                    client.ensure_tab(cfg.spreadsheet_id, PANEL_TAB, hidden=True)
                    client.write_panel(cfg.spreadsheet_id, PANEL_TAB,
                                       verify_mod.panel_rows(cfg, st))
                    print("  status panel updated in %s" % PANEL_TAB)
                except Exception as err:
                    print("  status panel not updated: %s" % str(err)[:100])

            st.record_summary(run_id, summary)
        except Exception as err:
            st.finish_run(run_id, "error", written, disagreed, str(err))
            raise
    st.close()
    return exit_code

def cmd_sync(args):
    if not args.write:
        print("no --write given, running as a dry run")
    return cmd_plan(args, write=bool(args.write))

def cmd_console(args):

    from . import webui
    return webui.serve(args, port=args.port, open_browser=not args.no_browser)

def cmd_pause(args):

    from . import control
    info = control.pause(args.db, args.client_id, "command line", args.reason)
    print("%s paused at %s" % (args.client_id, info["at"]))
    print("Runs will skip it. This does NOT remove the tool's access to the "
          "sheet: for that, take the service account off the sheet's share list.")
    return 0

def cmd_resume(args):
    from . import control
    control.resume(args.db, args.client_id)
    print("%s resumed. The next scheduled run will include it." % args.client_id)
    return 0

def cmd_status(args):

    from . import status as status_mod
    path = args.out or os.path.join(ROOT, "data", "status.html")
    log_path = os.path.join(ROOT, "data", "logs",
                            "opsync-%s.log" % time.strftime("%Y-%m-%d"))

    reports = [status_mod.gather(cfg, st, log_path=log_path)
               for _cid, cfg, st in _clients_with_store(args)]

    starts = [status_mod._parse(r["last"]["started_at"])
              for r in reports if r.get("last") and r["last"]["started_at"]]
    oldest_last = min(starts) if starts and None not in starts else None
    tail, trouble = status_mod.log_tail(log_path, since=oldest_last)
    status_mod.write(path, reports, log_lines=tail, log_trouble=trouble)
    for r in reports:
        missed = max(0, r["expected_today"] - r["runs_today"])
        note = "none missed" if not missed else "%d MISSED" % missed
        if r.get("failed_unrecovered"):

            unrec = len(r.get("failed_unrecovered") or [])
            if unrec:
                note += ", %d STEP FAILURE(S) NOT YET RECOVERED" % unrec
        print("%-12s %-10s last run %s, %d runs today, %s"
              % (r["cfg"].id, r["state"], r["ago"], r["runs_today"], note))
    print("written to %s" % path)
    return 0

def cmd_baseline(args):

    from . import sheets
    configs = _configs(args)
    if not args.credentials:
        raise SystemExit("baseline needs Google credentials")
    for cid, cfg in sorted(configs.items()):
        sc = sheets.SheetsClient(args.credentials, read_only=True)
        tabs = {}
        for tab_cfg in cfg.tabs:
            tab = sc.read_tab(cfg.spreadsheet_id, tab_cfg.name,
                              cfg.header_row, cfg.first_data_row)
            tabs[tab_cfg.name] = {"values": tab.values}
            print("%-22s %d rows x %d columns" % (
                tab_cfg.name, len(tab.values), len(tab.headers)))
        path = os.path.join(ROOT, "data", "snapshots", "%s-sheet.json" % cid)
        payload = {"source": "Sheets API baseline, %s" % _stamp(), "tabs": tabs}
        with io.open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False))
        print("baseline written to %s" % path)
    return 0

def _stamp():
    import time
    return time.strftime("%Y-%m-%d %H:%M")

def cmd_verify(args):

    from . import heyreach, sheets, verify
    exit_code = 0

    for cid, cfg, st in _clients_with_store(args):
        if not args.credentials:
            raise SystemExit("verify needs Google credentials to read the live sheet")
        sc = sheets.SheetsClient(args.credentials, read_only=True)
        result = verify.Result()

        result.say("== 1. cells that changed since the pre-write snapshot ==")
        verify.diff_against_snapshot(
            cfg, sc, os.path.join(ROOT, "data", "snapshots", "%s-sheet.json" % cid),
            st, result)

        result.say("")
        result.say("== 2. coverage: the database against what HeyReach reports ==")
        api = heyreach.Client(_api_key(cfg), store=None, client_id=cid)
        verify.coverage(cfg, api, st, result)

        result.say("")
        result.say("== 3. provenance: written labels traced to their evidence ==")
        verify.provenance(cfg, sc, st, result)

        result.say("")
        result.say("== 4. acceptance dates: what the sheet has against what HeyReach knows ==")
        verify.acceptance_gaps(cfg, sc, st, result)

        result.say("")
        result.say("== 5. formula health, including tabs nothing touches ==")

        verify.formula_health(cfg, sc, verify.all_tab_names(cfg, sc), result)

        for line in result.lines:
            print(line)
        print("")
        if result.problems:
            print("VERDICT: %d problem(s) found." % len(result.problems))
            exit_code = 2
        else:
            print("VERDICT: clean. Nothing changed that was not recorded, coverage "
                  "matches HeyReach, every written label traces to a message, and "
                  "no formula is in error.")
    st.close()
    return exit_code

def cmd_report(args):

    import datetime

    trouble = 0

    for cid, cfg, st in _clients_with_store(args):
        runs = st.db.execute(
            "SELECT id, started_at, finished_at, mode, status, cells_written, "
            "disagreements, detail FROM sync_runs WHERE client_id=? "
            "ORDER BY id DESC LIMIT 200", (cid,)).fetchall()

        if not runs:
            print("%s: no runs recorded yet" % cid)
            continue

        cutoff = (datetime.datetime.now() - datetime.timedelta(hours=24)).isoformat()
        recent = [r for r in runs if (r["started_at"] or "") >= cutoff]
        failed = [r for r in recent if r["status"] not in ("ok", None)]
        unfinished = [r for r in recent if not r["finished_at"]]
        written = sum(r["cells_written"] or 0 for r in recent)

        print("== %s ==" % cfg.name)
        print("last run          %s, %s, wrote %d, to look at %d"
              % (runs[0]["started_at"], runs[0]["status"],
                 runs[0]["cells_written"] or 0, runs[0]["disagreements"] or 0))
        print("last 24 hours     %d runs, %d cells written" % (len(recent), written))

        if failed:
            trouble += len(failed)
            print("FAILED OR REFUSED %d run(s):" % len(failed))
            for r in failed:
                print("   %s  %-8s %s" % (r["started_at"], r["status"],
                                          (r["detail"] or "")[:90]))
        else:
            print("failures          none")

        if unfinished:
            print("did not finish    %d run(s), which usually means the machine "
                  "slept mid-run" % len(unfinished))

        sources = st.label_source_counts(cid)
        total = sum(sources.values()) or 1
        print("message labels    %s  (%.0f%% matched against the Drips copy)"
              % (sources, 100.0 * sources.get("drips", 0) / total))

        gaps = st.db.execute(
            "SELECT COUNT(*) n FROM messages WHERE client_id=? AND direction='out' "
            "AND step_label IS NULL", (cid,)).fetchone()["n"]
        print("unlabelled        %d outbound messages, expected to be the sender's "
              "own replies and conversations outside any campaign" % gaps)

        print("")
        print("If anything above looks wrong, the next command is:")
        print("   py -m opsync verify --client %s" % cid)

    st.close()
    return 2 if trouble else 0

def main(argv=None):

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config-dir", default=DEFAULT_CONFIG_DIR)
    common.add_argument("--db", default=DEFAULT_DB)
    common.add_argument("--credentials",
                        default=os.environ.get("OPSYNC_CREDENTIALS", ""))
    common.add_argument("--client")

    parser = argparse.ArgumentParser(prog="opsync", parents=[common])
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("doctor", parents=[common])
    sub.add_parser("discover", parents=[common])
    fetch = sub.add_parser("fetch", parents=[common])
    fetch.add_argument("--relabel-all", action="store_true",
                       help="re-check every stored message against the Drips copy, "
                            "not just the unresolved ones. Run this after the "
                            "campaign copy changes.")
    sub.add_parser("plan", parents=[common])
    sync = sub.add_parser("sync", parents=[common])
    sync.add_argument("--write", action="store_true",
                      help="actually write to the sheet. Without this it is a dry run.")
    sub.add_parser("queue", parents=[common])
    headroom = sub.add_parser("headroom", parents=[common])
    headroom.add_argument("--write", action="store_true",
                          help="insert rows to make room. Without this it only measures.")
    st_p = sub.add_parser("status", parents=[common])
    st_p.add_argument("--out", help="where to write the page. Defaults to data/status.html")
    sub.add_parser("report", parents=[common])
    sub.add_parser("verify", parents=[common])
    sub.add_parser("baseline", parents=[common])
    con = sub.add_parser("console", parents=[common])
    con.add_argument("--port", type=int, default=8787)
    con.add_argument("--no-browser", action="store_true")
    pz = sub.add_parser("pause", parents=[common])
    pz.add_argument("client_id")
    pz.add_argument("--reason", default="")
    rz = sub.add_parser("resume", parents=[common])
    rz.add_argument("client_id")

    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 1

    return {
        "doctor": cmd_doctor,
        "discover": cmd_discover,
        "fetch": cmd_fetch,
        "plan": cmd_plan,
        "sync": cmd_sync,
        "queue": cmd_queue,
        "headroom": cmd_headroom,
        "status": cmd_status,
        "report": cmd_report,
        "verify": cmd_verify,
        "baseline": cmd_baseline,
        "console": cmd_console,
        "pause": cmd_pause,
        "resume": cmd_resume,
    }[args.command](args)

if __name__ == "__main__":
    sys.exit(main())
