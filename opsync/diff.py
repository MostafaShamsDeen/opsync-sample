from __future__ import unicode_literals

import collections

FILL = "fill"
AGREE = "agree"
DISAGREE = "disagree"
SKIP = "skip"

MACHINE = "machine"
SHARED = "shared"
HUMAN = "human"

REPORT = "report"

CONVENTION = "convention"

FILL_ONLY = "fill"

Change = collections.namedtuple(
    "Change", "row column header outcome current desired ownership note")

def _norm(value):

    if value is None:
        return ""
    if hasattr(value, "strftime"):
        return value.strftime("%d %b %y")
    return str(value).strip()

def compare_row(row_number, current, desired, ownership):

    changes = []
    for header, want in desired.items():
        own = ownership.get(header, HUMAN)
        have = _norm(current.get(header))
        want_s = _norm(want)

        if own == HUMAN:
            outcome, note = SKIP, "human owned"
        elif want_s == "":
            outcome, note = SKIP, "nothing to write"
        elif have == "":
            if own == REPORT:
                outcome, note = SKIP, "report only, blank left alone"
            else:

                outcome, note = FILL, ""
        elif have == want_s:
            outcome, note = AGREE, ""
        elif own == FILL_ONLY:
            outcome, note = SKIP, "differs, but this column only fills blanks"
        else:
            outcome, note = DISAGREE, "sheet has %r, source says %r" % (have, want_s)

        changes.append(Change(row_number, None, header, outcome, have, want_s, own, note))
    return changes

class Plan(object):

    def __init__(self, client, spreadsheet_id, tab):
        self.client = client
        self.spreadsheet_id = spreadsheet_id
        self.tab = tab
        self.changes = []

    def add(self, changes):
        self.changes.extend(changes)

    @property
    def fills(self):
        return [c for c in self.changes if c.outcome == FILL]

    @property
    def disagreements(self):
        return [c for c in self.changes if c.outcome == DISAGREE]

    def counts(self):
        out = collections.Counter(c.outcome for c in self.changes)
        return {k: out.get(k, 0) for k in (FILL, AGREE, DISAGREE, SKIP)}

    def check(self, max_cells, allowed_spreadsheets):

        problems = []
        if self.spreadsheet_id not in allowed_spreadsheets:
            problems.append(
                "spreadsheet %s is not in the allowlist" % self.spreadsheet_id)
        if len(self.fills) > max_cells:
            problems.append(
                "plan writes %d cells, cap is %d. Raise the cap deliberately or "
                "look at why this run found so much" % (len(self.fills), max_cells))
        return problems

    @property
    def actionable_disagreements(self):

        return [c for c in self.disagreements
                if c.ownership not in (REPORT, CONVENTION)]

    def by_header(self, changes):
        out = collections.Counter(c.header for c in changes)
        return out.most_common()

    def summary(self):
        c = self.counts()
        actionable = len(self.actionable_disagreements)
        reported = c[DISAGREE] - actionable
        return ("%s / %s: fill %d, agree %d, disagree %d "
                "(to look at %d, report-only columns %d), skip %d") % (
            self.client, self.tab, c[FILL], c[AGREE], c[DISAGREE],
            actionable, reported, c[SKIP])
