from __future__ import unicode_literals

H_NAME = "Full Name"
H_URL = "LinkedIn URL"
H_COMPANY = "Company Name"
H_DATE = "Date Stamp"
H_CAMPAIGN = "Campaign Name"

class StagingReport(object):
    def __init__(self):
        self.rows_read = 0
        self.matched = 0
        self.unmatched = 0
        self.ambiguous = 0
        self.no_campaign = 0
        self.invite_dates = 0
        self.accepted_dates = 0

        self.new_invite_dates = 0
        self.new_accepted_dates = 0

    @property
    def contributed(self):

        return self.new_invite_dates + self.new_accepted_dates

    def lines(self):
        return [
            "staging rows read  %d" % self.rows_read,
            "  matched to a lead %d" % self.matched,
            "  no lead found     %d" % self.unmatched,
            "  ambiguous         %d" % self.ambiguous,
            "  campaign unknown  %d" % self.no_campaign,
            "invite dates set   %d  (new to us %d)"
            % (self.invite_dates, self.new_invite_dates),
            "accepted dates set %d  (new to us %d)"
            % (self.accepted_dates, self.new_accepted_dates),
            "staging contributed %d date(s) this run that we did not already have"
            % self.contributed,
        ]

def _campaigns_by_name(store, client_id):
    rows = store.db.execute(
        "SELECT id, name FROM campaigns WHERE client_id=?", (client_id,)).fetchall()
    return dict((r["name"], r["id"]) for r in rows if r["name"])

def ingest(cfg, store, tabs, report=None):

    report = report or StagingReport()
    by_name = _campaigns_by_name(store, cfg.id)

    for field, records in (("invite_sent_at", tabs.get("invite_sent") or []),
                           ("accepted_at", tabs.get("accepted") or [])):
        for row in records:
            url = (row.get(H_URL) or "").strip()
            name = (row.get(H_NAME) or "").strip()
            company = (row.get(H_COMPANY) or "").strip()
            stamp = (row.get(H_DATE) or "").strip()
            campaign_name = (row.get(H_CAMPAIGN) or "").strip()
            if not stamp:
                continue
            report.rows_read += 1

            lead_id, how = store.find_lead(cfg.id, url, name, company)
            if how == "ambiguous":
                report.ambiguous += 1
                continue
            if lead_id is None:
                report.unmatched += 1
                continue
            report.matched += 1

            campaign_row = by_name.get(campaign_name)
            if campaign_row is None:

                rows_for_lead = store.campaigns_of_lead(lead_id)
                if not rows_for_lead:
                    report.no_campaign += 1
                    continue
                campaign_row = rows_for_lead[0]["id"]

            existing = store.db.execute(
                "SELECT %s AS v FROM lead_campaign WHERE lead_id=? AND campaign_id=?"
                % field, (lead_id, campaign_row)).fetchone()

            if existing and existing["v"] and existing["v"] <= stamp:
                continue

            store.db.execute(
                "INSERT INTO lead_campaign (lead_id, campaign_id, %s) VALUES (?,?,?) "
                "ON CONFLICT(lead_id, campaign_id) DO UPDATE SET %s=excluded.%s"
                % (field, field, field), (lead_id, campaign_row, stamp))
            store.db.commit()

            observed_col = ("observed_sent_at" if field == "invite_sent_at"
                            else "observed_accepted_at")
            held = store.db.execute(
                "SELECT %s AS obs FROM lead_campaign WHERE lead_id=? AND campaign_id=?"
                % observed_col, (lead_id, campaign_row)).fetchone()
            was_new = not (existing and existing["v"]) and not (held and held["obs"])

            if field == "invite_sent_at":
                report.invite_dates += 1
                report.new_invite_dates += int(was_new)
            else:
                report.accepted_dates += 1
                report.new_accepted_dates += int(was_new)

    return report
