from __future__ import unicode_literals

QUEUE_TAB = "Reply Queue"

COLUMNS = [
    ("replied",   "Replied",   90),
    ("lead",      "Lead",      170),
    ("company",   "Company",   170),
    ("persona",   "Persona",   140),
    ("row",       "Row",       55),
    ("after",     "After",     70),
    ("reply",     "Reply",     420),
    ("sentiment", "Sentiment", 105),
    ("status",    "Status",    200),
    ("done",      "Done",      60),
    ("latest",    "Latest",    90),
    ("msgs",      "Msgs",      55),
    ("thread",    "Thread",    260),
    ("profile",   "Profile",   220),
    ("lead_key",  "lead key",  130),
]

HEADERS = [h for _, h, _ in COLUMNS]
INDEX = dict((k, i) for i, (k, _, _) in enumerate(COLUMNS))
HEADER_OF = dict((k, h) for k, h, _ in COLUMNS)

SENTIMENT = "sentiment"

DONE = "done"
DONE_YES = "yes"

CHOICES = ["Positive", "Neutral", "Negative", "Ignore"]

DEFAULT_BLOCKS = {
    "Positive": "Positive After",
    "Neutral": "Neutral",
    "Negative": "Negative",
}

PARTS = ("Message", "Date", "Reply")

class QueueError(Exception):
    pass

def _listed(items):

    items = list(items)
    if len(items) < 2:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]

def block_headers(band):

    return [band + " / " + part for part in PARTS]

def all_block_headers(blocks):
    out = []
    for band in blocks.values():
        out.extend(block_headers(band))
    return out

def build(store, cfg, persona_of_lead=None):

    from . import projection

    contacted = store.campaign_contacted(cfg.id)

    tz = getattr(cfg, "timezone_offset_minutes", 0)
    fmt = getattr(cfg, "date_format", None)
    zero = getattr(cfg, "date_strip_leading_zero", None)
    rows = []
    for lead in store.repliers(cfg.id):
        lead_id = lead["id"]
        thread = store.thread_for_lead(lead_id)
        inbound = [m for m in thread if m["direction"] == "in"]
        if not inbound:
            continue

        first = inbound[0]
        last = inbound[-1]
        after = _step_before(thread, first["sent_at"])

        persona = ""
        if persona_of_lead is not None:
            persona = persona_of_lead(lead_id) or ""

        rows.append({
            "lead_id": lead_id,
            "first_reply_at": first["sent_at"],
            "replied": projection.format_date(first["sent_at"], tz, fmt, zero),
            "lead": lead["full_name"] or "",
            "company": lead["company"] or "",
            "persona": persona,
            "row": "",
            "after": after or "",
            "reply": _clean(first["body"]),
            "status": "",
            "latest": projection.format_date(last["sent_at"], tz, fmt, zero),
            "msgs": str(len(inbound)),
            "thread": _render_thread(thread, tz, fmt, zero),
            "profile": lead["linkedin_url"] or "",
            "lead_key": lead["lead_key"],
            "campaign_contact": lead_id in contacted,
        })

    rows.sort(key=lambda r: r["first_reply_at"], reverse=True)
    return rows

def actionable(rows):

    return [r for r in rows
            if str(r.get("persona") or "").strip() and r.get("campaign_contact")]

def held_back(rows):

    workable = set(id(r) for r in actionable(rows))
    return [r for r in rows if id(r) not in workable]

def settled(rows, info_by_lead):

    open_rows, done_rows = [], []
    for row in rows:
        info = info_by_lead.get(row["lead_id"]) or {}
        recorded = info.get("recorded")
        agrees = info.get("reply") == "agree"
        complete = bool(recorded) and getattr(recorded, "complete", False)
        if agrees or complete:
            done_rows.append(row)
        else:
            open_rows.append(row)

    if done_rows and open_rows:
        settled_keys = set(
            (_norm_person(r), r.get("persona") or "", _clean(r.get("reply") or ""),
             r.get("replied") or "")
            for r in done_rows)
        still_open, also_done = [], []
        for row in open_rows:
            key = (_norm_person(row), row.get("persona") or "",
                   _clean(row.get("reply") or ""), row.get("replied") or "")
            if key in settled_keys and key[2]:
                row["duplicate_of_recorded"] = True
                also_done.append(row)
            else:
                still_open.append(row)
        open_rows, done_rows = still_open, done_rows + also_done
    return open_rows, done_rows

def _norm_person(row):

    return " ".join((row.get("lead") or "").lower().split())

def retired(fresh, parsed):

    keys = set(r["lead_key"] for r in fresh)
    removable, keep = [], []
    for key, entry in parsed.items():
        if key in keys:
            continue
        if str(entry.get(SENTIMENT) or "").strip():
            keep.append(entry)
        else:
            removable.append(entry)
    return removable, keep

def _step_before(thread, when):

    best = None
    for msg in thread:
        if msg["direction"] != "out":
            continue
        if msg["sent_at"] >= when:
            break
        if msg["step_label"]:
            best = msg["step_label"]
    return best

def _clean(text):

    if not text:
        return ""
    out = " ".join(str(text).split())
    if out[:1] in ("=", "+", "-", "@"):
        out = "'" + out
    return out[:4000]

def _render_thread(thread, tz, fmt=None, zero=None):
    from . import projection
    parts = []
    for msg in thread:
        who = "them" if msg["direction"] == "in" else "us"
        stamp = projection.format_date(msg["sent_at"], tz, fmt, zero)
        body = " ".join((msg["body"] or "").split())[:300]
        parts.append("%s %s: %s" % (stamp, who, body))
    return _clean(" || ".join(parts))

def header_layout(values):

    if not values:
        return {}, list(HEADERS), []
    seen = {}
    duplicated = []
    for index, cell in enumerate(values[0]):
        text = str(cell).strip()
        if not text:
            continue
        if text in seen:
            duplicated.append(text)
        else:
            seen[text] = index
    missing = [h for h in HEADERS if h not in seen]
    return seen, missing, duplicated

def missing_header_writes(values):

    layout, missing, _ = header_layout(values)
    if not values or not layout:
        return []
    nxt = max(layout.values()) + 1
    out = []
    for header in missing:
        out.append((1, nxt, header))
        nxt += 1
    return out

def parse_grid(values):

    layout, missing, duplicated = header_layout(values)
    if not values:
        return {}, {}, ["queue tab is empty"]
    if missing:
        return {}, layout, ["queue tab is missing column(s): %s"
                            % ", ".join(missing)]
    if duplicated:
        return {}, layout, ["queue tab has %s more than once, so the tool "
                            "cannot tell which is which"
                            % ", ".join(sorted(set(duplicated)))]

    out = {}
    problems = []
    cols = dict((key, layout[HEADER_OF[key]]) for key, _, _ in COLUMNS)
    for offset, raw in enumerate(values[1:], start=2):
        if not any(str(c).strip() for c in raw):
            continue
        record = {}
        for key, index in cols.items():
            record[key] = str(raw[index]).strip() if index < len(raw) else ""
        key = record["lead_key"]
        if not key:
            problems.append("row %d has no lead key and was ignored" % offset)
            continue
        if key in out:
            problems.append("row %d repeats lead key %s" % (offset, key))
            continue
        record["_row"] = offset
        record["_cols"] = cols
        out[key] = record
    return out, layout, problems

def decisions(parsed):

    out = {}
    for key, record in parsed.items():
        choice = record.get(SENTIMENT, "").strip().title()
        if choice in CHOICES:
            out[key] = choice
    return out

def reply_for(cfg, row, sentiment):

    blocks = getattr(cfg, "reply_blocks", None) or DEFAULT_BLOCKS
    band = blocks.get(sentiment)
    if not band:
        return None
    return {
        band + " / Message": row.get("after", ""),
        band + " / Date": row.get("replied", ""),
        band + " / Reply": row.get("reply", ""),
    }

def plan_grid(fresh, parsed, layout=None):

    cols = dict((key, layout[HEADER_OF[key]]) for key, _, _ in COLUMNS) \
        if layout else dict((key, INDEX[key]) for key, _, _ in COLUMNS)
    width = max(cols.values()) + 1

    updates = []
    appends = []
    for row in fresh:
        existing = parsed.get(row["lead_key"])
        if existing is None:
            blank = [""] * width
            for key, index in cols.items():
                blank[index] = row.get(key, "")
            appends.append(blank)
            continue
        for key, _, _ in COLUMNS:
            if key == SENTIMENT:
                continue
            new = row.get(key, "")
            if str(existing.get(key, "")) != str(new):
                updates.append((existing["_row"], existing["_cols"][key], new))
    return updates, appends

def _same(a, b):

    return " ".join(str(a or "").split()).lower() == \
           " ".join(str(b or "").split()).lower()

class Entry(object):

    def __init__(self, sentiment, cells):
        self.sentiment = sentiment
        self.message, self.date, self.reply = cells
        self.filled = len([c for c in cells if str(c or "").strip()])
        self.blocks = 1
        self.differs = []

    @property
    def complete(self):
        return self.filled == 3

    def compare(self, row):

        out = []
        if row.get("after") and not _same(self.message, row.get("after")):
            out.append("step")
        if not _same(self.date, row.get("replied")):
            out.append("date")
        if not _same(self.reply, row.get("reply")):
            out.append("reply")
        self.differs = out
        return out

def recorded_entry(cfg, current):

    blocks = getattr(cfg, "reply_blocks", None) or DEFAULT_BLOCKS
    found = []
    for sentiment in sorted(blocks):
        cells = [current.get(h, "") for h in block_headers(blocks[sentiment])]
        if any(str(c or "").strip() for c in cells):
            found.append(Entry(sentiment, cells))
    if not found:
        return None
    entry = found[0]
    entry.blocks = len(found)
    return entry

def resolve(sentiment, persona, sheet_row, outcome, wrote, refused=None,
            recorded=None, require_match=False):

    if refused:
        return "not written: %s" % refused, ""

    if recorded and sheet_row and recorded.blocks > 1:
        return ("%s row %s has entries in more than one block, which needs a "
                "person" % (persona, sheet_row), "")

    if recorded and sheet_row and recorded.complete:
        differs = recorded.differs
        note = ""
        if differs:
            note = " (%s differ%s from the inbox)" % (
                _listed(differs), "" if len(differs) > 1 else "s")
        if require_match and differs:
            return ("%s row %s has a %s entry but the %s do not match the inbox"
                    % (persona, sheet_row, recorded.sentiment,
                       _listed(differs)), "")
        if sentiment and sentiment != recorded.sentiment and sentiment != "Ignore":
            return ("%s row %s already has a %s entry, so nothing was written"
                    % (persona, sheet_row, recorded.sentiment), DONE_YES)
        return ("already recorded on %s row %s as %s%s"
                % (persona, sheet_row, recorded.sentiment, note), DONE_YES)

    if recorded and sheet_row and not sentiment:

        missing = [name for name, value in (
            ("message", recorded.message), ("date", recorded.date),
            ("reply", recorded.reply)) if not str(value or "").strip()]
        return ("part of a %s entry is on %s row %s, missing the %s. Pick a "
                "sentiment and the run will fill it"
                % (recorded.sentiment, persona, sheet_row,
                   _listed(missing)), "")

    if not sentiment:
        return "waiting for a sentiment", ""
    if sentiment == "Ignore":
        return "ignored by the AC", DONE_YES
    if not persona:
        return "no campaign attributed, nowhere to write it", ""
    if not sheet_row:
        return "no row for this lead on %s" % persona, ""
    if outcome == "agree":
        return "already recorded on %s row %s" % (persona, sheet_row), DONE_YES
    if outcome == "fill":
        if wrote:
            return "written to %s row %s" % (persona, sheet_row), DONE_YES
        return "ready to write to %s row %s" % (persona, sheet_row), ""
    if outcome == "nothing":
        return "nothing to record: the reply text is empty", ""

    return ("%s row %s has an entry the run did not complete"
            % (persona, sheet_row), "")

def status_line(*args, **kwargs):

    return resolve(*args, **kwargs)[0]
