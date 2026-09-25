from __future__ import unicode_literals

import calendar
import time
import hashlib

from . import identity, labeller

def _hash(text):
    return hashlib.sha1((text or "").encode("utf-8")).hexdigest()[:16]

def _profile_fields(profile):

    if not profile:
        return {}
    email = (profile.get("emailAddress")
             or profile.get("enrichedEmailAddress")
             or profile.get("customEmailAddress"))
    name = " ".join(x for x in (profile.get("firstName"),
                                profile.get("lastName")) if x).strip()
    return {
        "linkedin_url": profile.get("profileUrl"),
        "full_name": name or None,
        "company": profile.get("companyName"),
        "title": profile.get("position") or profile.get("headline"),
        "email": email,
        "location": profile.get("location"),
    }

JUST_MISSED_MARGIN_SECONDS = 2 * 3600

def invite_time_just_missed(lead, before, last_look):

    if not last_look:
        return None
    if before not in ("ABSENT", None, "None"):
        return None
    if lead.get("leadConnectionStatus") not in ("ConnectionSent", "ConnectionAccepted"):
        return None
    if lead.get("leadMessageStatus") not in (None, "None"):
        return None
    last = lead.get("lastActionTime")
    if not last:
        return None
    created = lead.get("creationTime")
    if created and str(last)[:19] < str(created)[:19]:
        return None
    try:
        t_last = calendar.timegm(time.strptime(str(last)[:19], "%Y-%m-%dT%H:%M:%S"))
        t_look = calendar.timegm(time.strptime(str(last_look)[:19], "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return None
    if t_last < t_look - JUST_MISSED_MARGIN_SECONDS:
        return None
    return last

class FetchReport(object):
    def __init__(self):
        self.campaigns_seen = []
        self.campaigns_unmapped = []
        self.leads = 0
        self.leads_by_sender = {}
        self.leads_skipped_sender = 0
        self.leads_no_identity = 0
        self.acceptances_observed = 0
        self.sends_observed = 0
        self.sends_dated = 0
        self.conversations = 0
        self.conversations_unattached = 0
        self.conversations_matched_by_name = 0
        self.conversations_ambiguous = 0
        self.leads_in_several_personas = 0
        self.messages_in = 0
        self.messages_out = 0
        self.labelled = 0
        self.unresolved = 0

    def lines(self):
        out = [
            "campaigns mapped   %d" % len(self.campaigns_seen),
            "leads stored       %d" % self.leads,
            "  skipped, sender  %d" % self.leads_skipped_sender,
            "  skipped, no id   %d" % self.leads_no_identity,
            "conversations      %d  (matched by name %d, ambiguous %d, "
            "unattached to any campaign %d)"
            % (self.conversations, self.conversations_matched_by_name,
               self.conversations_ambiguous, self.conversations_unattached),
            "outbound messages  %d  (labelled %d, unresolved %d)"
            % (self.messages_out, self.labelled, self.unresolved),
            "inbound messages   %d" % self.messages_in,
        ]
        if self.acceptances_observed or self.sends_observed:
            out.append("watched happening this run: %d invites sent, %d accepted. "
                       "Dated from polling, no webhook involved"
                       % (self.sends_observed, self.acceptances_observed))
        if self.sends_dated:
            out.append("invite dates read from HeyReach's last action: %d, for "
                       "requests sent since OpSync last looked" % self.sends_dated)
        if self.leads_in_several_personas:
            out.append("leads in more than one persona %d, matched against both "
                       "sets of copy" % self.leads_in_several_personas)
        if self.leads_by_sender:
            out.append("leads by sender    %s" % ", ".join(
                "%s %d" % (k, v) for k, v in sorted(self.leads_by_sender.items())))
        if self.campaigns_unmapped:
            out.append("")
            out.append("CAMPAIGNS IN THE WORKSPACE WITH NO PERSONA TAB IN CONFIG:")
            for c in self.campaigns_unmapped:
                out.append("  %s  %s  (%s leads)" % (c["id"], c["name"], c["total"]))
            out.append("  Their leads have nowhere to go. Add them to the client "
                       "config or confirm they are deliberately out of scope.")
        return out

def fetch_client(cfg, api, store, templates_by_persona=None, report=None):
    report = report or FetchReport()
    templates_by_persona = templates_by_persona or {}

    store.upsert_client(cfg.id, cfg.name, cfg.workspace_id)

    last_look = store.last_fetch_utc(cfg.id)
    started = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())

    tab_of = {}
    for tab in cfg.tabs:
        for cid in tab.campaign_ids:
            tab_of[str(cid)] = tab.name

    campaign_rows = {}
    for camp in api.campaigns():
        cid = str(camp.get("id"))
        stats = camp.get("progressStats") or {}
        if cid not in tab_of:
            report.campaigns_unmapped.append({
                "id": cid, "name": camp.get("name"),
                "total": stats.get("totalUsers")})
            continue
        persona = tab_of[cid]
        campaign_rows[cid] = store.upsert_campaign(
            cfg.id, cid, camp.get("name"), persona)
        report.campaigns_seen.append(cid)

    senders = set(str(s) for s in cfg.sender_ids)

    for cid, campaign_row in campaign_rows.items():
        for lead in api.leads_in_campaign(int(cid)):

            sender = str(lead.get("linkedInSenderId") or "")
            who = lead.get("linkedInSenderFullName") or sender
            report.leads_by_sender[who] = report.leads_by_sender.get(who, 0) + 1
            if senders and sender not in senders:
                report.leads_skipped_sender += 1
                continue

            fields = _profile_fields(lead.get("linkedInUserProfile"))
            key = identity.lead_key(fields.get("linkedin_url"),
                                    fields.get("full_name"),
                                    fields.get("company"))
            if not key:
                report.leads_no_identity += 1
                continue

            lead_id = store.upsert_lead(cfg.id, key, fields)
            conn = lead.get("leadConnectionStatus")

            before = store.previous_status(lead_id, campaign_row)

            witnessed = store.observe_status(lead_id, campaign_row, conn)
            if witnessed == "ConnectionAccepted":
                report.acceptances_observed += 1
            elif witnessed == "ConnectionSent":
                report.sends_observed += 1

            store.set_lead_campaign(
                lead_id, campaign_row,
                invite_sent_at=None,
                accepted_at=None,
                status=conn)

            when = invite_time_just_missed(lead, before, last_look)
            if when and store.date_invite_from_last_action(
                    lead_id, campaign_row, when,
                    observed_this_run=(witnessed == "ConnectionSent")):
                report.sends_dated += 1
            report.leads += 1

    for conv in api.conversations():
        report.conversations += 1
        profile = conv.get("correspondentProfile") or {}
        fields = _profile_fields(profile)

        lead_id, how = store.find_lead(cfg.id, fields.get("linkedin_url"),
                                       fields.get("full_name"),
                                       fields.get("company"))
        if how == "ambiguous":
            report.conversations_ambiguous += 1
            continue
        if lead_id is None:
            key = identity.lead_key(fields.get("linkedin_url"),
                                    fields.get("full_name"),
                                    fields.get("company"))
            if not key:
                report.leads_no_identity += 1
                continue
            lead_id = store.upsert_lead(cfg.id, key, fields)
        elif how == "name":
            report.conversations_matched_by_name += 1

        rows = store.campaigns_of_lead(lead_id)
        if not rows:

            report.conversations_unattached += 1
            campaign_row = None
            templates = []
        else:
            campaign_row = rows[0]["id"]
            personas = []
            for r in rows:
                if r["persona_tab"] and r["persona_tab"] not in personas:
                    personas.append(r["persona_tab"])
            if len(personas) > 1:
                report.leads_in_several_personas += 1
            templates = []
            for persona in personas:
                templates.extend(templates_by_persona.get(persona, []))

        for msg in conv.get("messages") or []:
            outbound = msg.get("sender") == "ME"
            body = msg.get("body") or ""
            if not outbound:
                report.messages_in += 1
                store.add_message(cfg.id, lead_id, campaign_row,
                                  msg.get("createdAt"), "in", body,
                                  _hash(body), None, None, None)
                continue

            report.messages_out += 1
            label, source, detail = labeller.label_message(body, templates)
            if label:
                report.labelled += 1
            else:
                report.unresolved += 1
            store.add_message(cfg.id, lead_id, campaign_row,
                              msg.get("createdAt"), "out", body,
                              _hash(body), label, source, detail)

    store.record_fetch(cfg.id, started)
    return report

class RelabelReport(object):

    def __init__(self):
        self.checked = 0
        self.newly_labelled = 0
        self.changed = 0
        self.cleared = 0
        self.still_unresolved = 0
        self.examples = []

    def lines(self):
        out = ["relabel checked    %d" % self.checked,
               "  newly labelled   %d" % self.newly_labelled,
               "  label changed    %d" % self.changed,
               "  label withdrawn  %d" % self.cleared,
               "  still unresolved %d" % self.still_unresolved]
        for e in self.examples[:10]:
            out.append("  %s" % e)
        return out

def relabel(cfg, store, templates_by_persona, only_unresolved=True, report=None):

    report = report or RelabelReport()

    for row in store.outbound_for_relabel(cfg.id, only_unresolved):
        report.checked += 1
        templates = templates_by_persona.get(row["persona_tab"], [])
        label, source, detail = labeller.label_message(row["body"], templates)
        before = row["step_label"]

        if label == before:
            if label is None:
                report.still_unresolved += 1
            continue

        if before is None:
            report.newly_labelled += 1
            report.examples.append(
                "now %s: %s" % (label, " ".join((row["body"] or "")[:60].split())))
        elif label is None:

            report.cleared += 1
            report.examples.append("withdrew %s from a message that no longer matches" % before)
        else:
            report.changed += 1
            report.examples.append("%s -> %s, reported not overwritten" % (before, label))

        store.set_message_label(row["id"], label, source, detail)

    return report
