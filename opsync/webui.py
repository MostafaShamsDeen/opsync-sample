from __future__ import unicode_literals

import io
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid

try:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import urlparse, parse_qs
except ImportError:
    from BaseHTTPServer import BaseHTTPRequestHandler
    from urlparse import urlparse, parse_qs
    ThreadingHTTPServer = None

BIND = "127.0.0.1"

class Jobs(object):

    def __init__(self):
        self.lock = threading.Lock()
        self.current = None
        self.history = []

    def busy(self):
        with self.lock:
            return self.current is not None and self.current["state"] == "running"

    def start(self, label, steps, cwd):

        with self.lock:
            if self.current is not None and self.current["state"] == "running":
                return None
            job = {"id": uuid.uuid4().hex[:8], "label": label, "steps": steps,
                   "state": "running", "started": time.strftime("%H:%M:%S"),
                   "lines": [], "code": None}
            self.current = job
        threading.Thread(target=self._run, args=(job, cwd), daemon=True).start()
        return job

    def _run(self, job, cwd):
        env = dict(os.environ)
        env["PYTHONWARNINGS"] = "ignore"
        env["PYTHONIOENCODING"] = "utf-8"
        code = 0
        held = _take_run_lock(cwd)
        if held is False:
            job["lines"].append("The hourly run is in progress. Try again when it finishes.")
            job["steps"] = []
            code = 1
        for argv in job["steps"]:
            job["lines"].append("$ opsync " + " ".join(argv[3:]))
            try:
                proc = subprocess.Popen(
                    argv, cwd=cwd, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, env=env, universal_newlines=True)
                for line in iter(proc.stdout.readline, ""):
                    line = line.rstrip()
                    if line:
                        job["lines"].append(line)
                        del job["lines"][:-400]
                proc.stdout.close()
                code = proc.wait()
            except Exception as exc:
                job["lines"].append("could not start: %s" % exc)
                code = -1
            if code != 0:
                job["lines"].append("step failed, exit %s. Stopping here." % code)
                break
        if held:
            held.close()
        job["code"] = code
        job["state"] = "done" if code == 0 else "failed"
        with self.lock:
            self.history.insert(0, {k: job[k] for k in
                                    ("id", "label", "state", "started", "code")})
            del self.history[8:]

JOBS = Jobs()

def _take_run_lock(root):

    try:
        import fcntl
    except ImportError:
        return None
    path = os.path.join(root, "data", "run.lock")
    try:
        fh = open(path, "a")
    except (IOError, OSError):
        return None
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (IOError, OSError):
        fh.close()
        return False
    return fh

def _snapshot(args):

    from . import config as config_mod, control, status as status_mod, store as store_mod

    configs = config_mod.load_all(args.config_dir)
    paused = control.read(args.db)["paused"]
    log_path = os.path.join(args.db, "logs")
    out = []
    for cid in sorted(configs):
        cfg = configs[cid]
        st = store_mod.Store(store_mod.path_for(cid, args.db))
        try:
            g = status_mod.gather(cfg, st, log_path=log_path)

            info = {
                "id": cid,
                "name": cfg.name,
                "tabs": [t.name for t in cfg.tabs],
                "paused": paused.get(cid),
                "state": g["state"],
                "last_text": g["ago"],
                "runs_today": g["runs_today"],
                "expected_today": g["expected_today"],
                "failures_today": g["failures_today"],
                "cells_today": g["cells_today"],
                "rows_created": g["rows_created"],
                "watched": g["watched"],
                "failed_steps": g.get("failed_unrecovered") or [],
                "disagreements": (g["last"]["disagreements"] if g["last"] else 0) or 0,
            }
            info.update(_client_extras(st, cid))
            out.append(info)
        finally:
            st.close()
    return out

def _one(row):
    return row[0] if row else 0

def _client_extras(st, cid):

    db = st.db
    runs = [dict(r) for r in db.execute(
        "SELECT id, started_at, finished_at, mode, status, cells_written, "
        "disagreements, detail FROM sync_runs ORDER BY id DESC LIMIT 25")]
    by_reason = dict((r[0] or "?", r[1]) for r in db.execute(
        "SELECT reason, COUNT(*) FROM write_log GROUP BY reason"))
    replies_total = _one(db.execute(
        "SELECT COUNT(DISTINCT lead_id) FROM messages WHERE direction='in'").fetchone())
    return {
        "runs": runs,
        "summary": _latest_summary(db),
        "writes_total": _one(db.execute("SELECT COUNT(*) FROM write_log").fetchone()),
        "writes_by_reason": by_reason,
        "leads": _one(db.execute("SELECT COUNT(*) FROM leads").fetchone()),
        "messages": _one(db.execute("SELECT COUNT(*) FROM messages").fetchone()),
        "replies": replies_total,
        "campaigns": [dict(r) for r in db.execute(
            "SELECT heyreach_campaign_id, name, persona_tab FROM campaigns "
            "ORDER BY name")],
        "db_bytes": _db_size(st),
    }

def _latest_summary(db):

    row = db.execute("SELECT summary FROM sync_runs WHERE summary IS NOT NULL "
                     "ORDER BY id DESC LIMIT 1").fetchone()
    if not row or not row[0]:
        return None
    try:
        return json.loads(row[0])
    except ValueError:
        return None

def _db_size(st):
    try:
        page = _one(st.db.execute("PRAGMA page_count").fetchone())
        size = _one(st.db.execute("PRAGMA page_size").fetchone())
        return page * size
    except Exception:
        return 0

def _run_detail(args, client_id, run_id):

    from . import control, store as store_mod
    st = store_mod.Store(store_mod.path_for(client_id, args.db))
    try:
        run = st.db.execute(
            "SELECT id, started_at, finished_at, mode, status, cells_written, "
            "disagreements, detail FROM sync_runs WHERE id=?", (run_id,)).fetchone()
        if run is None:
            return None
        plan = control.rollback_plan(st, run_id)
        return {"run": dict(run), "plan": plan,
                "sample": plan["cells"][:120], "client_id": client_id}
    finally:
        st.close()

def _logo():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logo-b64.txt")
    try:
        with io.open(path, "r", encoding="utf-8") as fh:
            return fh.read().strip()
    except (IOError, OSError):
        return ""

def _page():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "console.html")
    with io.open(path, "r", encoding="utf-8") as fh:
        return fh.read().replace("__LOGO__", _logo())

def _rollback_apply(args, client_id, run_id):

    from . import config as config_mod, control, sheets, store as store_mod

    cfg = config_mod.load_all(args.config_dir).get(client_id)
    if cfg is None:
        return {"error": "no such client: %s" % client_id}
    if not args.credentials:
        return {"error": "undo needs Google credentials"}

    allowed = config_mod.allowlist(config_mod.load_all(args.config_dir))
    st = store_mod.Store(store_mod.path_for(client_id, args.db))
    try:
        plan = control.rollback_plan(st, run_id)
    finally:
        st.close()
    if not plan["cells"]:
        return {"error": "run %s wrote nothing" % run_id}

    for cell in plan["cells"]:
        if cell["spreadsheet_id"] not in allowed:
            return {"error": "run %s touched a spreadsheet not on the allowlist"
                             % run_id}

    client = sheets.SheetsClient(args.credentials, read_only=False)
    cleared = 0
    left = []
    by_tab = {}
    for cell in plan["cells"]:
        by_tab.setdefault((cell["spreadsheet_id"], cell["tab"]), []).append(cell)
    for (sheet_id, tab_name), cells in sorted(by_tab.items()):
        tab = client.read_tab(sheet_id, tab_name, cfg.header_row, cfg.first_data_row)
        to_clear, kept = control.rollback_cells(cells, tab)
        left.extend(kept)
        if to_clear:
            cleared += client.update_scattered(
                sheet_id, tab_name, [(row, col, "") for row, col in to_clear])
    changed = [c for c, why in left if why == "changed since by a person"]
    return {"ok": True, "cleared": cleared, "planned": plan["cells_to_clear"],
            "left_changed_by_person": len(changed),
            "left_other": len(left) - len(changed),
            "left_examples": ["%s row %d %s" % (c["tab"], c["row"], c["header"])
                              for c in changed[:10]]}

class Handler(BaseHTTPRequestHandler):

    server_version = "opsync-console"
    ARGS = None

    def log_message(self, fmt, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        raw = body.encode("utf-8") if not isinstance(body, bytes) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")

        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(raw)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, default=str))

    def _body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except ValueError:
            return {}

    def _local_only(self):

        host = (self.client_address or ("",))[0]
        return host in ("127.0.0.1", "::1", "localhost")

    def do_GET(self):
        if not self._local_only():
            return self._json({"error": "local requests only"}, 403)
        path = urlparse(self.path).path
        try:
            if path in ("/", "/index.html"):
                return self._send(200, _page(), "text/html; charset=utf-8")
            if path == "/api/state":
                job = JOBS.current
                return self._json({
                    "clients": _snapshot(self.ARGS),
                    "job": {k: job[k] for k in
                            ("id", "label", "state", "started", "lines", "code")} if job else None,
                    "history": JOBS.history,
                })
            if path.startswith("/api/runs/"):
                cid = path[len("/api/runs/"):]
                for c in _snapshot(self.ARGS):
                    if c["id"] == cid:
                        return self._json({"runs": c["runs"]})
                return self._json({"error": "no such client"}, 404)
            if path.startswith("/api/run/"):
                rest = path[len("/api/run/"):].split("/")
                if len(rest) != 2:
                    return self._json({"error": "bad path"}, 400)
                detail = _run_detail(self.ARGS, rest[0], int(rest[1]))
                if detail is None:
                    return self._json({"error": "no such run"}, 404)
                return self._json(detail)
            return self._json({"error": "not found"}, 404)
        except Exception as exc:
            return self._json({"error": str(exc)}, 500)

    def do_POST(self):
        if not self._local_only():
            return self._json({"error": "local requests only"}, 403)

        if self.headers.get("X-OpSync") != "1":
            return self._json({"error": "missing console header"}, 403)
        path = urlparse(self.path).path
        data = self._body()
        args = self.ARGS
        cid = (data.get("client") or "").strip()
        try:
            from . import control
            if path == "/api/pause":
                control.pause(args.db, cid, "console", (data.get("reason") or "").strip())
                return self._json({"ok": True})
            if path == "/api/resume":
                control.resume(args.db, cid)
                return self._json({"ok": True})
            if path == "/api/run":
                if JOBS.busy():
                    return self._json({"error": "a run is already going. One at a time."}, 409)
                root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                steps = [
                    [sys.executable, "-m", "opsync", "fetch", "--client", cid],
                    [sys.executable, "-m", "opsync", "sync", "--client", cid,
                     "--write"],
                ]
                job = JOBS.start("fetch and sync: %s" % cid, steps, root)
                if job is None:
                    return self._json({"error": "a run is already going"}, 409)
                return self._json({"ok": True, "job": job["id"]})
            if path == "/api/onboard/inspect":
                return self._json(_inspect(args, data))
            if path == "/api/onboard/commit":
                return self._json(_commit(args, data))
            if path == "/api/rollback":
                if (data.get("confirm") or "").strip().upper() != "UNDO":
                    return self._json({"error": "not confirmed"}, 400)
                return self._json(_rollback_apply(args, cid, int(data.get("run"))))
            return self._json({"error": "not found"}, 404)
        except Exception as exc:
            return self._json({"error": str(exc)}, 500)

def serve(args, port=8787, open_browser=True):

    Handler.ARGS = args
    if ThreadingHTTPServer is None:
        raise SystemExit("the console needs Python 3")
    httpd = ThreadingHTTPServer((BIND, port), Handler)
    url = "http://%s:%d/" % (BIND, port)
    print("OpSync console on %s" % url)
    print("Reachable from this machine only. Ctrl-C to stop.")
    if open_browser:
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nconsole stopped")
    finally:
        httpd.server_close()
    return 0

SHEET_ID = re.compile(r"/spreadsheets/d/([a-zA-Z0-9-_]+)")
SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{1,30}$")

def _sheet_id(text):
    text = (text or "").strip()
    m = SHEET_ID.search(text)
    if m:
        return m.group(1)
    return text if re.match(r"^[a-zA-Z0-9-_]{20,}$", text) else None

def _inspect(args, data):

    from . import config as config_mod, heyreach, onboard, sheets

    key = (data.get("api_key") or "").strip()
    sid = _sheet_id(data.get("sheet") or "")
    cid = (data.get("client_id") or "").strip().lower()
    if not key:
        return {"error": "an API key is needed"}
    if not sid:
        return {"error": "that does not look like a Google Sheets link"}
    if not SAFE_ID.match(cid):
        return {"error": "the short name must be lower case letters, numbers "
                         "and hyphens, e.g. acme-corp"}
    if cid in config_mod.load_all(args.config_dir):
        return {"error": "a client called %s already exists" % cid}
    if not args.credentials:
        return {"error": "no Google credentials configured"}

    api = heyreach.Client(key)
    try:
        api.check_key()
    except Exception as exc:
        return {"error": "that API key was refused: %s" % exc}

    sc = sheets.SheetsClient(args.credentials, read_only=True)
    try:
        meta = sc.api.spreadsheets().get(
            spreadsheetId=sid, fields="properties.title,sheets.properties.title"
        ).execute()
    except Exception:
        return {"error": "cannot open that sheet. Share it with the service "
                         "account as an Editor first, then try again."}

    title = meta.get("properties", {}).get("title", "")
    tab_names = [s["properties"]["title"] for s in meta.get("sheets", [])]

    persona, structure, skipped = {}, None, []
    for name in tab_names:
        if name.startswith("_") or name in ("Drips", "Charts", "Meetings"):
            continue
        grid = sc.read_grid(sid, name)
        rows = onboard.find_header_rows(grid) if grid else None
        if not rows:
            skipped.append(name)
            continue
        structure = structure or rows
        persona[name] = onboard.tab_identities(grid, rows[0], rows[1])
        persona[name] = (persona[name], grid, rows)
    if not persona:
        return {"error": "no tab on that sheet has both a Full Name and a "
                         "LinkedIn URL column, so none of them is a persona tab"}

    import collections as _c
    rowvotes = _c.Counter(rows for _ids, _g, rows in persona.values())
    header_row, first_data_row = rowvotes.most_common(1)[0][0]

    bandvotes = _c.Counter()
    for _ids, grid, rows in persona.values():
        b = onboard.invite_band(grid, rows[0])
        if b:
            bandvotes[b] += 1
    band = bandvotes.most_common(1)[0][0] if bandvotes else None

    samples = []
    if band:
        for _ids, grid, rows in persona.values():
            samples += onboard.column_values(grid, rows[0], rows[1], band, "Sent")
    fit = onboard.fit_date_format(samples)

    def _rows_on(name):
        ids = persona[name][0]
        return len(ids[0]) + len(ids[1])
    ordered = sorted(persona, key=lambda t: (-_rows_on(t), t))

    campaigns, senders = [], set()
    for c in api.campaigns():
        leads, n = [], 0
        for lead in api.leads_in_campaign(str(c["id"])):
            leads.append(onboard.lead_fields(lead))
            if lead.get("linkedInSenderId"):
                senders.add(str(lead["linkedInSenderId"]))
            n += 1
            if n >= 300:
                break
        campaigns.append({"id": c["id"], "name": (c.get("name") or "").strip(),
                          "status": c.get("status"),
                          "total": (c.get("progressStats") or {}).get("totalUsers"),
                          "leads": leads})

    mapping = onboard.propose_mapping(
        campaigns, dict((k, v[0]) for k, v in persona.items()))

    return {
        "ok": True,
        "client_id": cid,
        "sheet_id": sid,
        "sheet_title": title,
        "tabs": ordered,
        "tab_rows": dict((t, _rows_on(t)) for t in ordered),
        "skipped_tabs": skipped,
        "header_row": header_row,
        "first_data_row": first_data_row,
        "band": band,
        "date_format": {"spec": fit[0], "strip": bool(fit[1]),
                        "matched": fit[2], "of": fit[3]} if fit else None,
        "senders": sorted(senders),
        "mapping": mapping,
        "campaign_count": len(campaigns),
    }

def _template_config(args):

    import io as _io
    from . import config as config_mod
    configs = config_mod.load_all(args.config_dir)
    for cid in ("alpha-live", "charlie", "bravo"):
        path = os.path.join(args.config_dir, "%s.json" % cid)
        if cid in configs and os.path.exists(path):
            with _io.open(path, "r", encoding="utf-8") as fh:
                return json.load(fh)
    raise RuntimeError("no existing client to take the column map from")

def _secrets_path():

    return (os.environ.get("OPSYNC_ENV_FILE")
            or os.path.join(os.path.expanduser("~"), ".secrets", "opsync.env"))

def _store_key(client_id, api_key):

    import io as _io
    path = _secrets_path()
    name = "HEYREACH_API_KEY_%s" % client_id.upper().replace("-", "_")
    lines = []
    if os.path.exists(path):
        with _io.open(path, "r", encoding="utf-8-sig") as fh:
            lines = fh.read().splitlines()
    lines = [l for l in lines if not l.strip().startswith(name + "=")]
    lines.append("%s=%s" % (name, api_key))
    directory = os.path.dirname(path)
    if directory and not os.path.isdir(directory):
        os.makedirs(directory)
    with _io.open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return name

def _commit(args, data):

    import io as _io
    from . import config as config_mod, onboard, sheets

    cid = (data.get("client_id") or "").strip().lower()
    if not SAFE_ID.match(cid):
        return {"error": "bad short name"}
    if cid in config_mod.load_all(args.config_dir):
        return {"error": "a client called %s already exists" % cid}
    mapping = [m for m in (data.get("mapping") or []) if m.get("assigned")]
    if not mapping:
        return {"error": "no campaign has been assigned to a tab"}

    key = (data.get("api_key") or "").strip()
    if not key:
        return {"error": "the API key is needed to finish"}
    env_name = _store_key(cid, key)

    template = _template_config(args)
    fit = data.get("date_format") or None

    sc = sheets.SheetsClient(args.credentials, read_only=True)
    report_tab = onboard.find_report_tab(
        lambda t: sc.read_grid(data["sheet_id"], t),
        [t for t in (data.get("tabs") or [])
         if t not in set(m["assigned"] for m in mapping)])

    cfg = onboard.build_config(
        client_id=cid,
        name=(data.get("name") or cid).strip(),
        spreadsheet_id=data["sheet_id"],
        api_key_env=env_name,
        mapping=mapping,
        tabs=data.get("tabs") or [],
        header_row=int(data["header_row"]),
        first_data_row=int(data["first_data_row"]),
        band=data["band"],
        date_fit=(fit["spec"], fit["strip"]) if fit else None,
        offset=data.get("offset") or None,
        sender_ids=data.get("senders") or [],
        template=template,
        report_tab=report_tab)

    snapshot = {"source": "Sheets API baseline, taken at onboarding", "tabs": {}}
    for tab in sorted(set(m["assigned"] for m in mapping)):
        grid = sc.read_grid(cfg["spreadsheet_id"], tab)
        snapshot["tabs"][tab] = {"values": grid or []}
    snap_dir = os.path.join(args.db, "snapshots")
    if not os.path.isdir(snap_dir):
        os.makedirs(snap_dir)
    with _io.open(os.path.join(snap_dir, "%s-sheet.json" % cid), "w",
                  encoding="utf-8") as fh:
        fh.write(json.dumps(snapshot, ensure_ascii=False))

    path = os.path.join(args.config_dir, "%s.json" % cid)
    with _io.open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(cfg, indent=1, ensure_ascii=False, sort_keys=False))

    from . import control
    control.pause(args.db, cid, "onboarding",
                  "new client, paused until a dry run has been read")
    return {"ok": True, "config_path": path, "env_name": env_name,
            "tabs": sorted(snapshot["tabs"]), "paused": True}
