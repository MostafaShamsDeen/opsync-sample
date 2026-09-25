from __future__ import unicode_literals

import io
import time

OK_MINUTES = 90
LATE_MINUTES = 180

def _parse(stamp):

    if not stamp:
        return None
    text = str(stamp).strip().replace("T", " ")[:19]
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return time.mktime(time.strptime(text, fmt))
        except ValueError:
            continue
    return None

def _ago(seconds):

    if seconds is None:
        return "never"
    seconds = int(seconds)
    if seconds < 90:
        return "just now"
    if seconds < 3600:
        return "%d minutes ago" % (seconds // 60)
    if seconds < 86400:
        hours = seconds // 3600
        return "%d hour%s ago" % (hours, "" if hours == 1 else "s")
    days = seconds // 86400
    return "%d day%s ago" % (days, "" if days == 1 else "s")

def failed_steps_today(log_path, client_id, today=None):

    try:
        with io.open(log_path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except (IOError, OSError):
        return []
    today = today or time.strftime("%Y-%m-%d")
    hits = []
    for line in lines:
        if not line.startswith(today):
            continue
        if "STEP FAILED" not in line and "ABORT" not in line:
            continue

        if client_id and (" for %s " % client_id) not in line and "ABORT" not in line:
            continue
        hits.append(line.strip())
    return hits

def _failed_since(failure_line, last_run):

    if not last_run or not last_run["started_at"]:
        return True
    stamp = (failure_line or "")[:19]
    try:
        when = time.strptime(stamp, "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return True
    started = _parse(last_run["started_at"])
    if started is None:
        return True
    return time.mktime(when) > started

def gather(cfg, store, now=None, log_path=None):

    now = now or time.time()
    today = time.strftime("%Y-%m-%d", time.localtime(now))

    runs = store.db.execute(
        "SELECT started_at, finished_at, mode, status, cells_written, disagreements, "
        "detail, id FROM sync_runs WHERE client_id=? ORDER BY id DESC LIMIT 40",
        (cfg.id,)).fetchall()

    last = runs[0] if runs else None
    last_at = _parse(last["started_at"]) if last else None
    since = (now - last_at) if last_at else None

    if since is None:
        state = "never"
    elif since <= OK_MINUTES * 60:
        state = "ok"
    elif since <= LATE_MINUTES * 60:
        state = "late"
    else:
        state = "stale"

    day_runs = [r for r in runs
                if (r["started_at"] or "")[:10] == today and r["mode"] == "write"]
    failures = [r for r in day_runs if r["status"] not in ("ok", None)]

    midnight = time.mktime(time.strptime(today, "%Y-%m-%d"))
    starts = [r["started_at"] for r in runs if r["started_at"]]
    start_from = midnight
    if starts:
        first = min(starts)
        if first[:10] == today:
            try:
                born = time.mktime(time.strptime(first[:19], "%Y-%m-%dT%H:%M:%S"))
                start_from = max(midnight, born)
            except ValueError:
                pass

    elapsed_hours = int((now - start_from) // 3600)
    expected_today = max(0, elapsed_hours, len(day_runs))

    failed_steps = failed_steps_today(log_path, cfg.id, today) if log_path else []

    unrecovered = [f for f in failed_steps if _failed_since(f, last)]
    if unrecovered and state == "ok":
        state = "late"

    run_ids = [r["id"] for r in runs]
    cells_today = sum(r["cells_written"] or 0 for r in day_runs)

    written_by_column = []
    rows_created = 0
    if run_ids:
        marks = ",".join("?" * len(run_ids))
        written_by_column = store.db.execute(
            "SELECT header, COUNT(*) n FROM write_log WHERE run_id IN (%s) "
            "GROUP BY header ORDER BY n DESC LIMIT 8" % marks, run_ids).fetchall()
        rows_created = store.db.execute(
            "SELECT COUNT(DISTINCT tab || ':' || row) n FROM write_log "
            "WHERE run_id IN (%s) AND reason='new row'" % marks, run_ids).fetchone()["n"]

    import json
    summary = {}
    if last is not None:
        try:
            summary = json.loads(last["summary"]) or {}
        except (TypeError, ValueError, KeyError, IndexError):
            summary = {}

    watched = store.db.execute(
        "SELECT COUNT(*) n FROM lead_campaign lc JOIN leads l ON l.id=lc.lead_id "
        "WHERE l.client_id=? AND (lc.observed_accepted_at IS NOT NULL "
        "OR lc.observed_sent_at IS NOT NULL)", (cfg.id,)).fetchone()["n"]

    return {
        "cfg": cfg,
        "failed_steps": failed_steps,
        "failed_unrecovered": unrecovered,
        "state": state,
        "ago": _ago(since),
        "last": last,
        "runs": runs[:12],
        "runs_today": len(day_runs),
        "expected_today": expected_today,
        "failures_today": len(failures),
        "cells_today": cells_today,
        "cells_recent": sum(r["cells_written"] or 0 for r in runs),
        "rows_created": rows_created,
        "by_column": written_by_column,
        "watched": watched,
        "summary": summary,
    }

def log_tail(path, lines=40, since=None):

    try:
        with io.open(path, "r", encoding="utf-8", errors="replace") as fh:
            all_lines = fh.read().splitlines()
    except (IOError, OSError):
        return [], []
    trouble = [l for l in all_lines
               if ("FAILED" in l or "ABORT" in l or "REFUSED" in l
                   or "Traceback" in l or "not updated" in l)]
    if since:
        keep = []
        for l in trouble:
            try:
                when = time.mktime(time.strptime(l[:19], "%Y-%m-%d %H:%M:%S"))
            except (ValueError, TypeError):
                keep.append(l)
                continue
            if when > since:
                keep.append(l)
        trouble = keep
    return [l for l in all_lines if l.strip()][-lines:], trouble[-10:]

CSS = """
:root{--bg:#f4f5f7;--card:#fff;--line:#dfe3e8;--ink:#14171a;--ink2:#5b6570;
--ok:#1a7f4b;--ok-bg:#e8f6ee;--late:#8a5a00;--late-bg:#fdf3e0;
--bad:#a32316;--bad-bg:#fbeae8;--accent:#1f4e79;}
@media(prefers-color-scheme:dark){:root{--bg:#14171a;--card:#1c2024;--line:#2e3439;
--ink:#eef1f4;--ink2:#9aa5b0;--ok:#5fd39a;--ok-bg:#12301f;--late:#e0ad4e;
--late-bg:#32270f;--bad:#f08a7c;--bad-bg:#700013;--accent:#7fb3e0;}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font:14px/1.55 -apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
.wrap{max-width:1000px;margin:0 auto;padding:26px 18px 70px}
h1{font-size:21px;margin:0 0 3px}
.sub{color:var(--ink2);font-size:13px;margin:0 0 22px}
.card{background:var(--card);border:1px solid var(--line);border-radius:7px;
margin-bottom:18px;overflow:hidden}
.head{padding:15px 18px;border-bottom:1px solid var(--line)}
.head h2{font-size:16px;margin:0 0 2px}
.head .id{color:var(--ink2);font-size:12px;font-family:ui-monospace,Consolas,monospace}
.banner{padding:14px 18px;font-size:19px;font-weight:600;display:flex;
justify-content:space-between;align-items:baseline;flex-wrap:wrap;gap:8px}
.banner .when{font-size:13px;font-weight:400}
.ok{background:var(--ok-bg);color:var(--ok)}
.late{background:var(--late-bg);color:var(--late)}
.stale,.never{background:var(--bad-bg);color:var(--bad)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
gap:1px;background:var(--line)}
.cell{background:var(--card);padding:13px 18px}
.cell b{display:block;font-size:20px;font-variant-numeric:tabular-nums}
.cell span{color:var(--ink2);font-size:12px}
.cell.warn b{color:var(--bad)}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th,td{text-align:left;padding:7px 18px;border-top:1px solid var(--line)}
th{color:var(--ink2);font-weight:500;font-size:11px;text-transform:uppercase;
letter-spacing:.06em}
td.n{font-variant-numeric:tabular-nums;text-align:right}
td.bad{color:var(--bad)}
.sect{padding:11px 18px 4px;color:var(--ink2);font-size:11px;
text-transform:uppercase;letter-spacing:.06em;border-top:1px solid var(--line)}
.note{color:var(--ink2);font-size:12.5px;padding:0 18px 15px}
.foot{color:var(--ink2);font-size:12px;margin-top:26px;line-height:1.7}
.tw{overflow-x:auto}
pre.log{margin:0;padding:12px 18px;overflow-x:auto;font-size:11.5px;line-height:1.5;
font-family:ui-monospace,Consolas,monospace;color:var(--ink2);white-space:pre-wrap;
word-break:break-word}
tr.gap td{background:var(--bad-bg);color:var(--bad);font-weight:600;
text-align:center;letter-spacing:.02em}
"""

def _esc(text):
    return (str(text or "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))

def render(reports, now=None, log_lines=(), log_trouble=()):

    now = now or time.time()
    stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(now))
    worst = "ok"
    for r in reports:
        if r["state"] in ("stale", "never"):
            worst = "stale"
        elif r["state"] == "late" and worst == "ok":
            worst = "late"

    out = ["<!doctype html><html lang=en><head><meta charset=utf-8>",
           "<meta name=viewport content='width=device-width,initial-scale=1'>",
           "<title>OpSync status</title><style>%s</style></head><body>" % CSS,
           "<div class=wrap>",
           "<h1>OpSync status</h1>",
           "<p class=sub>Generated %s from the local database. "
           "This page does not refresh itself: if the time above is old, the "
           "machine that runs OpSync is not running it.</p>" % _esc(stamp)]

    for r in reports:
        cfg = r["cfg"]
        last = r["last"]
        headline = {"ok": "Running normally", "late": "A run is late",
                    "stale": "NOT RUNNING", "never": "Has never run"}[r["state"]]
        out.append("<div class=card>")
        out.append("<div class=head><h2>%s</h2><div class=id>%s &middot; %s</div></div>"
                   % (_esc(cfg.name), _esc(cfg.id), _esc(cfg.spreadsheet_id)))
        out.append("<div class='banner %s'><span>%s</span>"
                   "<span class=when>last run %s</span></div>"
                   % (r["state"], _esc(headline), _esc(r["ago"])))

        for line in r.get("failed_unrecovered", []):
            out.append("<div class='banner stale'><span>STEP FAILED</span>"
                       "<span class=when>%s</span></div>" % _esc(line))

        missed = max(0, r["expected_today"] - r["runs_today"])
        out.append("<div class=grid>")
        out.append("<div class=cell><b>%d</b><span>runs today, %d expected</span></div>"
                   % (r["runs_today"], r["expected_today"]))
        out.append("<div class='cell%s'><b>%d</b><span>missed today</span></div>"
                   % (" warn" if missed else "", missed))
        out.append("<div class='cell%s'><b>%d</b><span>failed runs today</span></div>"
                   % (" warn" if r["failures_today"] else "", r["failures_today"]))
        out.append("<div class=cell><b>%d</b><span>cells written today</span></div>"
                   % r["cells_today"])
        out.append("<div class=cell><b>%d</b><span>differences to look at</span></div>"
                   % ((last["disagreements"] or 0) if last else 0))
        out.append("<div class=cell><b>%d</b><span>dates found by watching</span></div>"
                   % r["watched"])
        out.append("</div>")

        sm = r.get("summary") or {}
        if sm.get("queue_rows") is not None:
            out.append("<div class=sect>Reply queue</div><div class=grid>")
            out.append("<div class=cell><b>%d</b><span>on the queue</span></div>"
                       % sm.get("queue_rows", 0))
            out.append("<div class='cell%s'><b>%d</b><span>waiting for a person"
                       "</span></div>"
                       % (" warn" if sm.get("queue_waiting") else "",
                          sm.get("queue_waiting", 0)))
            out.append("<div class=cell><b>%d</b><span>held back, no persona"
                       "</span></div>" % sm.get("queue_held_back", 0))
            out.append("</div>")

        tabs = sm.get("tabs") or []
        if tabs:
            out.append("<div class=sect>By persona tab, last run</div>"
                       "<div class=tw><table>")
            out.append("<tr><th>Tab</th><th class=n>Filled</th><th class=n>Agreed</th>"
                       "<th class=n>To look at</th><th class=n>Known convention</th>"
                       "<th class=n>New rows</th></tr>")
            for t in tabs:
                out.append("<tr><td>%s</td><td class=n>%s</td><td class=n>%s</td>"
                           "<td class=n>%s</td><td class=n>%s</td><td class=n>%s</td></tr>"
                           % (_esc(t.get("tab")), t.get("fill", 0), t.get("agree", 0),
                              t.get("to_look_at", 0), t.get("reported_only", 0),
                              t.get("new_rows", 0)))
            out.append("</table></div>")

            cols = []
            for t in tabs:
                for header, count in (t.get("by_column") or []):
                    cols.append((header, count, t.get("tab")))
            if cols:
                out.append("<div class=sect>Which columns disagree</div>"
                           "<div class=tw><table>")
                out.append("<tr><th>Column</th><th>Tab</th><th class=n>Rows</th></tr>")
                for header, count, tabname in sorted(cols, key=lambda x: -x[1])[:10]:
                    out.append("<tr><td>%s</td><td>%s</td><td class=n>%d</td></tr>"
                               % (_esc(header), _esc(tabname), count))
                out.append("</table></div>")

        out.append("<div class=sect>Recent runs</div><div class=tw><table>")
        out.append("<tr><th>Started</th><th>Took</th><th>Mode</th><th>Status</th>"
                   "<th class=n>Cells</th><th class=n>Differences</th></tr>")
        previous = None
        for run in r["runs"]:
            began = _parse(run["started_at"])

            if previous is not None and began is not None:
                gap = previous - began
                if gap > OK_MINUTES * 60:
                    out.append("<tr class=gap><td colspan=6>%s with no run</td></tr>"
                               % _esc(_ago(gap).replace(" ago", "")))
            previous = began
            ended = _parse(run["finished_at"])
            took = "%ds" % int(ended - began) if (began and ended) else ""
            bad = run["status"] not in ("ok", None)
            out.append("<tr><td>%s</td><td>%s</td><td>%s</td><td%s>%s</td>"
                       "<td class=n>%s</td><td class=n>%s</td></tr>"
                       % (_esc((run["started_at"] or "").replace("T", " ")),
                          took, _esc(run["mode"]),
                          " class=bad" if bad else "", _esc(run["status"]),
                          run["cells_written"] or 0, run["disagreements"] or 0))
        out.append("</table></div>")

        if r["by_column"]:
            out.append("<div class=sect>What it wrote recently</div><div class=tw><table>")
            out.append("<tr><th>Column</th><th class=n>Cells</th></tr>")
            for row in r["by_column"]:
                out.append("<tr><td>%s</td><td class=n>%d</td></tr>"
                           % (_esc(row["header"]), row["n"]))
            out.append("</table></div>")
            out.append("<p class=note>%d row(s) created across those runs.</p>"
                       % r["rows_created"])
        out.append("</div>")

    if log_lines or log_trouble:
        out.append("<div class=card><div class=head><h2>Today's run log</h2>"
                   "<div class=id>the only place that says why</div></div>")
        if log_trouble:
            out.append("<div class=sect>Lines worth reading</div><pre class=log>%s</pre>"
                       % _esc(chr(10).join(log_trouble)))
        out.append("<div class=sect>Last %d lines</div><pre class=log>%s</pre></div>"
                   % (len(log_lines), _esc(chr(10).join(log_lines))))

    out.append("<p class=foot><b>How to read this.</b> Green means it ran within "
               "the last %d minutes. Amber means a beat was missed. Red means it "
               "has not run for %d minutes or more and somebody should look.<br>"
               "<b>What this page cannot do.</b> It cannot tell anyone. It is "
               "generated after each run, so if the machine stops, the page stops "
               "updating and the time at the top stops moving. That is the signal."
               "</p>" % (OK_MINUTES, LATE_MINUTES))
    out.append("</div></body></html>")
    return "\n".join(out)

def write(path, reports, now=None, log_lines=(), log_trouble=()):
    with io.open(path, "w", encoding="utf-8") as fh:
        fh.write(render(reports, now, log_lines, log_trouble))
    return path
