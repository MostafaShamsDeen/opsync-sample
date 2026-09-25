from __future__ import unicode_literals

import re
import unicodedata

_SCHEME = re.compile(r"^https?://", re.I)
_HOST = re.compile(r"^([a-z0-9-]+\.)*linkedin\.com/", re.I)
_IN_PATH = re.compile(r"^(?:.*/)?in/([^/?#]+)", re.I)
_PUNCT = re.compile(r"[^a-z0-9]+")

_NAME_NOISE = (
    "mba", "phd", "md", "cpa", "pe", "pmp", "cfa", "esq", "jr", "sr",
    "ii", "iii", "iv", "ret", "usa", "usmc", "usn", "usaf",

    "cpsp", "ssh", "asc", "asce", "fasce", "wre", "bcwre", "cscp", "cpim",
    "cfp", "clu", "chfc", "cem", "cpe", "lssbb", "lssgb", "ccim", "sphr",

    "csp", "chst", "cusp", "stsc", "gsp", "cpm", "aia", "ncarb", "shrm",
    "cds", "asp", "dba", "leed", "mpa", "cfm", "cssgb", "rpa", "fma",
    "bsme", "cfe",
)

def normalise_url(url):

    if not url:
        return ""
    s = str(url).strip().lower()
    if not s:
        return ""
    s = _SCHEME.sub("", s)
    s = re.sub(r"^www\.", "", s)
    s = s.split("?", 1)[0].split("#", 1)[0]
    if not _HOST.match(s) and "linkedin.com/" not in s:
        return ""

    s = re.sub(r"^([a-z0-9-]+\.)*linkedin\.com/", "linkedin.com/", s)
    m = _IN_PATH.search(s)
    if not m:
        return ""
    slug = m.group(1).strip("/")
    if not slug:
        return ""
    return "linkedin.com/in/" + slug

def _fold(value):

    if value is None:
        return ""
    s = unicodedata.normalize("NFKD", str(value))
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().strip()
    return _PUNCT.sub(" ", s).strip()

def plain_name(name):

    folded = _fold(name)
    if not folded:
        return ""
    parts = [p for p in folded.split(" ") if p and p not in _NAME_NOISE]
    return " ".join(parts)

def name_key(name, company):

    n = plain_name(name)
    c = _fold(company)
    if not n or not c:
        return ""
    return n + "|" + c

def lead_key(url, name, company):

    u = normalise_url(url)
    if u:
        return "url:" + u
    k = name_key(name, company)
    if k:
        return "name:" + k
    return ""

class Index(object):

    def __init__(self):
        self._by_url = {}
        self._by_name = {}
        self._ambiguous_names = set()

        self._urlless_by_name = {}

        self._rows_by_plain_name = {}

    def add(self, row, url, name, company, title=None, location=None):
        u = normalise_url(url)
        if u and u not in self._by_url:
            self._by_url[u] = row
        plain = plain_name(name)
        if not u:
            if plain:
                self._urlless_by_name.setdefault(plain, []).append(row)
        if plain:
            self._rows_by_plain_name.setdefault(plain, []).append({
                "row": row, "url": u,
                "title": _fold(title or ""), "location": _fold(location or "")})
        k = name_key(name, company)
        if k:
            if k in self._by_name and self._by_name[k] != row:
                self._ambiguous_names.add(k)
            else:
                self._by_name[k] = row

    def find(self, url, name, company):

        u = normalise_url(url)
        if u and u in self._by_url:
            return self._by_url[u], "url"
        k = name_key(name, company)
        if k and k in self._ambiguous_names:
            return None, "ambiguous"
        if k and k in self._by_name:
            return self._by_name[k], "name"
        return None, None

    def probable_same_person(self, name, url, title, location):

        plain = plain_name(name)
        if not plain:
            return []
        mine = normalise_url(url)
        want_title = _fold(title or "")
        want_location = _fold(location or "")
        if not want_title and not want_location:
            return []
        out = []
        for rec in self._rows_by_plain_name.get(plain, []):
            if not rec["url"] or rec["url"] == mine:
                continue
            if ((want_title and rec["title"] == want_title)
                    or (want_location and rec["location"] == want_location)):
                out.append(rec["row"])
        return out

    def rows_named_without_url(self, name):

        plain = plain_name(name)
        if not plain:
            return []
        return list(self._urlless_by_name.get(plain, []))
