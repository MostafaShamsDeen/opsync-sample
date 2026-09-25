from __future__ import unicode_literals

import datetime
import re

from . import identity

DATE_FORMATS = [
    ("%d %b %y", True), ("%d %b %y", False),
    ("%b-%d-%y", False), ("%b-%d-%y", True),
    ("%d/%m/%Y", False), ("%m/%d/%Y", False),
    ("%Y-%m-%d", False), ("%d-%b-%y", False),
    ("%d %B %Y", True), ("%b %d, %Y", False),
]

INVITE_BANDS = ["LI Invites", "CR", "Connection Requests", "Invites"]

REQUIRED_COLUMNS = ["Full Name", "LinkedIn URL"]

def _fmt(value, spec, strip):
    out = value.strftime(spec)
    if strip:
        out = re.sub(r"(^|[^\d])0(\d)", r"\1\2", out)
    return out

def banded_headers(values, header_row):

    if not values or len(values) < header_row:
        return []
    band_row = values[header_row - 2] if header_row >= 2 else []
    sub_row = values[header_row - 1]
    out, current = [], ""
    for i, sub in enumerate(sub_row):
        band = (band_row[i] if i < len(band_row) else "").strip()
        if band:
            current = band
        out.append((current, (sub or "").strip()))
    return out

def looks_like_persona_tab(values, header_row=2):

    if not values or len(values) < header_row:
        return False
    subs = set(s for _, s in banded_headers(values, header_row))
    return all(c in subs for c in REQUIRED_COLUMNS)

def _is_countish(text):

    t = str(text or "").strip().replace(",", "")
    if not t:
        return False
    try:
        float(t)
        return True
    except ValueError:
        return False

def find_header_rows(values):

    for header_row in (2, 3, 1, 4):
        if not looks_like_persona_tab(values, header_row):
            continue
        subs = banded_headers(values, header_row)
        name_col = next((i for i, (_, s) in enumerate(subs) if s == "Full Name"), None)
        if name_col is None:
            continue
        for first in range(header_row + 1, min(header_row + 8, len(values) + 1)):
            row = values[first - 1] if first - 1 < len(values) else []
            cell = row[name_col] if name_col < len(row) else ""
            if str(cell).strip() and not _is_countish(cell):
                return header_row, first
        return header_row, header_row + 2
    return None

def invite_band(values, header_row):

    bands = set(b for b, _ in banded_headers(values, header_row))
    for candidate in INVITE_BANDS:
        if candidate in bands:
            return candidate
    for band in bands:
        if band and "Sent" in [s for b, s in banded_headers(values, header_row)
                               if b == band]:
            return band
    return None

def column_values(values, header_row, first_data_row, band, sub):

    cols = banded_headers(values, header_row)
    idx = next((i for i, (b, s) in enumerate(cols) if b == band and s == sub), None)
    if idx is None:
        return []
    out = []
    for row in values[first_data_row - 1:]:
        if idx < len(row):
            v = str(row[idx]).strip()
            if v:
                out.append(v)
    return out

def fit_date_format(samples, limit=400):

    samples = [s for s in samples if s][:limit]
    if not samples:
        return None
    best = None
    for spec, strip in DATE_FORMATS:
        matched = 0
        for s in samples:
            try:
                parsed = datetime.datetime.strptime(s, spec)
            except ValueError:
                continue
            if _fmt(parsed, spec, strip) == s:
                matched += 1
        if best is None or matched > best[2]:
            best = (spec, strip, matched, len(samples))
    if best is None or best[2] == 0:
        return None
    return best

def offset_sweep(pairs, minutes=(0, 60, 120, 180, 240, 300, -60, -120, -180,
                                -240, -300, -360, -420, -480)):

    if not pairs:
        return None
    scores = []
    for off in minutes:
        hit = 0
        for text, iso in pairs:
            try:
                when = datetime.datetime.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S")
            except (ValueError, TypeError):
                continue
            shifted = when + datetime.timedelta(minutes=off)
            for spec, strip in DATE_FORMATS:
                try:
                    if _fmt(shifted, spec, strip) == text:
                        hit += 1
                        break
                except ValueError:
                    continue
        scores.append((off, hit))
    scores.sort(key=lambda p: (-p[1], abs(p[0])))
    top, second = scores[0], (scores[1] if len(scores) > 1 else (None, 0))
    return {
        "offset": top[0],
        "matched": top[1],
        "of": len(pairs),
        "runner_up": second[0],
        "runner_up_matched": second[1],

        "decisive": top[1] > 0 and top[1] >= second[1] + max(3, len(pairs) // 20),
        "all": scores,
    }

def tab_identities(values, header_row, first_data_row):

    cols = banded_headers(values, header_row)
    def col(sub):
        return next((i for i, (_, s) in enumerate(cols) if s == sub), None)
    ni, ui, ci = col("Full Name"), col("LinkedIn URL"), col("Company Name")
    urls, names = set(), set()
    for row in values[first_data_row - 1:]:
        def g(i):
            return str(row[i]).strip() if i is not None and i < len(row) else ""
        if not g(ni):
            continue
        if g(ui):
            key = identity.normalise_url(g(ui))
            if key:
                urls.add(key)

        nk = identity.name_key(g(ni), g(ci))
        if nk:
            names.add(nk)
    return urls, names

def lead_fields(payload):

    profile = (payload or {}).get("linkedInUserProfile") or {}
    name = " ".join(x for x in (profile.get("firstName"),
                                profile.get("lastName")) if x).strip()
    return {
        "name": name,
        "company": profile.get("companyName") or "",
        "url": profile.get("profileUrl") or "",
    }

def overlap(campaign_leads, urls, names):

    usable = [l for l in campaign_leads
              if (l.get("url") or "").strip() or (l.get("name") or "").strip()]
    if not usable:
        return 0.0, 0, 0
    hits = 0
    for lead in usable:
        url = (lead.get("url") or "").strip()
        key = identity.normalise_url(url) if url else None
        if key and key in urls:
            hits += 1
            continue
        name = (lead.get("name") or "").strip()
        if not name:

            continue
        if identity.name_key(name, lead.get("company") or "") in names:
            hits += 1
    return hits / float(len(usable)), hits, len(usable)

def propose_mapping(campaigns, tabs):

    out = []
    for c in campaigns:
        scores = []
        for tab, (urls, names) in sorted(tabs.items()):
            share, hits, total = overlap(c["leads"], urls, names)
            scores.append({"tab": tab, "share": round(share * 100, 1),
                           "hits": hits, "of": total})
        scores.sort(key=lambda s: -s["share"])
        best = scores[0] if scores else None
        second = scores[1] if len(scores) > 1 else None
        out.append({
            "campaign_id": str(c["id"]),
            "campaign": c["name"],
            "proposed": best["tab"] if best and best["share"] >= 40 else None,
            "confidence": best["share"] if best else 0,
            "runner_up": second["tab"] if second else None,
            "runner_up_share": second["share"] if second else 0,
            "clear": bool(best and second and best["share"] >= 40
                          and best["share"] >= second["share"] * 2),
            "scores": scores,
        })
    return out

def find_report_tab(read_grid, tab_names):

    from . import responses
    for name in tab_names:
        try:
            grid = read_grid(name)
            if not grid:
                continue
            blocks = responses.map_blocks(grid)
        except Exception:
            continue
        found = [responses.room_for(blocks, s)[1]
                 for s in ("Positive", "Neutral", "Negative")]
        if all(b is not None for b in found):
            return name
    return ""

def build_config(client_id, name, spreadsheet_id, api_key_env, mapping, tabs,
                 header_row, first_data_row, band, date_fit, offset,
                 sender_ids, template, report_tab=""):

    by_tab = {}
    for m in mapping:
        if not m.get("assigned"):
            continue
        by_tab.setdefault(m["assigned"], []).append(int(m["campaign_id"]))
    cfg = {
        "id": client_id,
        "name": name,
        "workspace_id": None,
        "api_key_env": api_key_env,
        "spreadsheet_id": spreadsheet_id,
        "header_row": header_row,
        "first_data_row": first_data_row,
        "drips_tab": "Drips",
        "sender_ids": [str(s) for s in sender_ids],
        "tabs": dict((t, {"campaign_ids": sorted(ids)})
                     for t, ids in sorted(by_tab.items())),
        "columns": dict(template["columns"]),
        "ownership": dict(template["ownership"]),
    }
    cfg["columns"]["invite_sent"] = "%s / Sent" % band
    cfg["columns"]["invite_accepted"] = "%s / Accepted" % band
    own = {}
    for header, rule in template["ownership"].items():
        if header.endswith("/ Sent") or header.endswith("/ Accepted"):
            continue
        own[header] = rule
    own["%s / Sent" % band] = "convention"
    own["%s / Accepted" % band] = "machine"
    cfg["ownership"] = own
    if date_fit:
        cfg["date_format"] = date_fit[0]
        cfg["date_strip_leading_zero"] = bool(date_fit[1])
    if offset:
        cfg["timezone_offset_minutes"] = offset
    cfg["max_new_rows_per_run"] = 60
    if report_tab:
        cfg["report_tab"] = report_tab
    return cfg
