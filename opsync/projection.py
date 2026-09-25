from __future__ import unicode_literals

import datetime

DATE_FORMAT = "%d %b %y"
STRIP_LEADING_ZERO = True

def format_date(value, offset_minutes=0, fmt=None, strip_leading_zero=None):

    if not value:
        return ""
    if isinstance(value, datetime.datetime):
        dt = value
    else:
        text = str(value).strip()
        if not text:
            return ""
        text = text.replace("Z", "").split(".")[0]

        for parse_fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                dt = datetime.datetime.strptime(text, parse_fmt)
                break
            except ValueError:
                dt = None
        if dt is None:
            return text
    if offset_minutes:
        dt = dt + datetime.timedelta(minutes=offset_minutes)
    stamp = dt.strftime(fmt or DATE_FORMAT)
    if STRIP_LEADING_ZERO if strip_leading_zero is None else strip_leading_zero:
        stamp = stamp.lstrip("0")
    return stamp

def project_lead(cfg, lead, messages, reply=None):

    tz = getattr(cfg, "timezone_offset_minutes", 0)
    fmt = getattr(cfg, "date_format", None)
    zero = getattr(cfg, "date_strip_leading_zero", None)

    def when(value):
        return format_date(value, tz, fmt, zero)

    desired = {
        cfg.header("full_name"): lead["full_name"] or "",
        cfg.header("linkedin_url"): lead["linkedin_url"] or "",
        cfg.header("company"): lead["company"] or "",
        cfg.header("title"): lead["title"] or "",
        cfg.header("email"): lead["email"] or "",
        cfg.header("invite_sent"): when(lead["invite_sent_at"]),
        cfg.header("invite_accepted"): when(lead["accepted_at"]),
    }

    if "location" in cfg.columns:
        try:
            desired[cfg.header("location")] = lead["location"] or ""
        except (KeyError, IndexError):
            pass

    seen = {}
    for msg in messages:
        label = msg["step_label"]
        if not label:
            continue
        try:
            band = int(label.split(" ")[1])
        except (IndexError, ValueError):
            continue
        if band not in seen:
            seen[band] = (label, msg["sent_at"])

    for band, (label, sent_at) in seen.items():
        if band > cfg.li_bands:

            continue
        desired[cfg.li_header(band, "type")] = label
        desired[cfg.li_header(band, "date")] = when(sent_at)

    if reply:
        desired.update(reply)

    return desired

def overflow_bands(cfg, messages):

    out = []
    for msg in messages:
        label = msg["step_label"]
        if not label:
            continue
        try:
            band = int(label.split(" ")[1])
        except (IndexError, ValueError):
            continue
        if band > cfg.li_bands:
            out.append(label)
    return out

def ownership_map(cfg, headers):

    return dict((h, cfg.ownership_of(h)) for h in headers)

def apply_mirrors(cfg, tab, current, desired):

    for target, source in getattr(cfg, "mirror", {}).items():
        if not (tab.has(target) and tab.has(source)):
            continue
        value = str(current.get(source) or "").strip()
        if not value:
            value = str(desired.get(source) or "").strip()
        if value:
            desired[target] = value
    return desired
