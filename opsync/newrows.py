from __future__ import unicode_literals

class RowPlan(object):

    def __init__(self, client, spreadsheet_id, tab, first_row):
        self.client = client
        self.spreadsheet_id = spreadsheet_id
        self.tab = tab
        self.first_row = first_row
        self.additions = []
        self.skipped = {}

        self.clashes = []

        self.same_people = []

    def add(self, lead_id, values):
        self.additions.append((lead_id, values))

    def skip(self, reason):
        self.skipped[reason] = self.skipped.get(reason, 0) + 1

    def clash(self, name, rows):
        self.clashes.append((name, list(rows)))
        self.skip("a row already carries this name without a URL")

    def same_person(self, name, rows):
        self.same_people.append((name, list(rows)))
        self.skip("already on the sheet under a different URL form")

    def __len__(self):
        return len(self.additions)

    def rows(self):

        return [(self.first_row + i, values)
                for i, (_lead, values) in enumerate(self.additions)]

    def check(self, max_rows, allowed_spreadsheets):

        problems = []
        if self.spreadsheet_id not in allowed_spreadsheets:
            problems.append("spreadsheet %s is not in the allowlist"
                            % self.spreadsheet_id)
        if len(self.additions) > max_rows:
            problems.append(
                "would add %d rows to %s, cap is %d. Adding some of them would "
                "look like it worked, so none are added: find out what changed "
                "upstream first" % (len(self.additions), self.tab, max_rows))
        if self.first_row < 2:
            problems.append("refusing to write at row %d" % self.first_row)
        seen = set()
        for lead_id, _values in self.additions:
            if lead_id in seen:
                problems.append("lead %s would be added twice" % lead_id)
            seen.add(lead_id)
        return problems

    def summary(self):
        parts = ["%s / %s: %d new row(s) from row %d"
                 % (self.client, self.tab, len(self.additions), self.first_row)]
        for reason, n in sorted(self.skipped.items()):
            parts.append("    skipped %-34s %d" % (reason, n))
        for name, rows in self.clashes[:10]:
            parts.append(
                "    LOOK AT   %s may already be on row %s. Put the LinkedIn "
                "URL in that row and it will be filled automatically"
                % (name, ", ".join(str(r) for r in rows)))
        for name, rows in self.same_people[:10]:
            parts.append(
                "    LOOK AT   %s looks like the person already on row %s "
                "under a different LinkedIn URL form, so no row was added. "
                "If they really are two people, say so and one will be made"
                % (name, ", ".join(str(r) for r in rows)))
        return "\n".join(parts)

def _field(lead, key):

    try:
        return lead[key]
    except (KeyError, IndexError, TypeError):
        return None

def campaign_ids_of(lead):

    raw = ""
    try:
        raw = lead["campaign_ids"] or ""
    except (KeyError, IndexError, TypeError):
        raw = ""
    return set(c.strip() for c in str(raw).split(",") if c.strip())

def first_free_row(tab):

    last = tab.first_data_row - 1
    for offset in range(tab.first_data_row - 1, len(tab.values)):
        raw = tab.values[offset]
        if any((c or "").strip() for c in raw):
            last = offset + 1
    return last + 1

def plan(cfg, tab_cfg, tab, leads, index, project, first_row=None):

    live = set(tab_cfg.live_campaign_ids)

    identity_headers = set()
    for logical in ("full_name", "linkedin_url", "company", "title", "email",
                    "location"):
        try:
            identity_headers.add(cfg.header(logical))
        except Exception:
            pass
    rp = RowPlan(cfg.id, cfg.spreadsheet_id, tab_cfg.name,
                 first_row if first_row is not None else first_free_row(tab))

    for lead in leads:
        row, how = index.find(lead["linkedin_url"], lead["full_name"],
                              lead["company"])
        if row is not None:
            continue
        if how == "ambiguous":

            rp.skip("identity is ambiguous on the sheet")
            continue
        if not campaign_ids_of(lead) & live:
            rp.skip("only in a historical campaign")
            continue
        if not (lead["full_name"] or "").strip():
            rp.skip("no name to put in the row")
            continue
        if not (lead["linkedin_url"] or "").strip():

            rp.skip("no LinkedIn URL to identify it by")
            continue

        clash = index.rows_named_without_url(lead["full_name"])
        if clash:
            rp.clash(lead["full_name"], clash)
            continue

        same = index.probable_same_person(
            lead["full_name"], lead["linkedin_url"],
            _field(lead, "title"), _field(lead, "location"))
        if same:
            rp.same_person(lead["full_name"], same)
            continue

        values = dict((h, v) for h, v in project(lead).items()
                      if tab.has(h) and str(v or "").strip())

        for target, source in getattr(cfg, "mirror", {}).items():
            if tab.has(target) and str(values.get(source) or "").strip():
                values.setdefault(target, values[source])

        if not set(values) - identity_headers:
            rp.skip("nothing has happened to them yet")
            continue

        rp.add(lead["id"], values)

    return rp
