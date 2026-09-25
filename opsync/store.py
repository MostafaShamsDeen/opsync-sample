from __future__ import unicode_literals

import hashlib
import json
import os
import sqlite3
import time

from . import identity

SCHEMA = """
CREATE TABLE IF NOT EXISTS clients (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    workspace_id  TEXT,
    active        INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS campaigns (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id             TEXT NOT NULL,
    heyreach_campaign_id  TEXT NOT NULL,
    name                  TEXT,
    persona_tab           TEXT,
    UNIQUE (client_id, heyreach_campaign_id)
);

CREATE TABLE IF NOT EXISTS senders (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id           TEXT NOT NULL,
    heyreach_sender_id  TEXT NOT NULL,
    name                TEXT,
    UNIQUE (client_id, heyreach_sender_id)
);

CREATE TABLE IF NOT EXISTS leads (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id      TEXT NOT NULL,
    lead_key       TEXT NOT NULL,
    url_key        TEXT,
    name_key       TEXT,
    linkedin_url   TEXT,
    full_name      TEXT,
    company        TEXT,
    title          TEXT,
    email          TEXT,
    location       TEXT,
    first_seen     TEXT,
    last_seen      TEXT,
    UNIQUE (client_id, lead_key)
);

CREATE TABLE IF NOT EXISTS lead_campaign (
    lead_id        INTEGER NOT NULL,
    campaign_id    INTEGER NOT NULL,
    invite_sent_at TEXT,
    observed_sent_at TEXT,
    accepted_at    TEXT,
    observed_accepted_at TEXT,
    replied_at     TEXT,
    status         TEXT,
    PRIMARY KEY (lead_id, campaign_id)
);

CREATE TABLE IF NOT EXISTS messages (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id    TEXT NOT NULL,
    lead_id      INTEGER NOT NULL,
    campaign_id  INTEGER,
    sent_at      TEXT NOT NULL,
    direction    TEXT NOT NULL,
    body         TEXT,
    body_hash    TEXT NOT NULL,
    step_label   TEXT,
    label_source TEXT,
    label_detail TEXT,
    UNIQUE (client_id, lead_id, sent_at, body_hash)
);

CREATE TABLE IF NOT EXISTS corrections (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id      TEXT NOT NULL,
    lead_id        INTEGER,
    field          TEXT NOT NULL,
    machine_value  TEXT,
    human_value    TEXT,
    seen_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sync_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id       TEXT NOT NULL,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    mode            TEXT NOT NULL,
    cells_written   INTEGER DEFAULT 0,
    disagreements   INTEGER DEFAULT 0,
    status          TEXT,
    detail          TEXT
);

CREATE TABLE IF NOT EXISTS write_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        INTEGER NOT NULL,
    spreadsheet_id TEXT NOT NULL,
    tab           TEXT NOT NULL,
    row           INTEGER NOT NULL,
    header        TEXT NOT NULL,
    value         TEXT,
    reason        TEXT
);

CREATE TABLE IF NOT EXISTS raw_payloads (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id   TEXT NOT NULL,
    endpoint    TEXT NOT NULL,
    fetched_at  TEXT NOT NULL,
    request     TEXT,
    body        TEXT,
    body_hash   TEXT
);

CREATE TABLE IF NOT EXISTS payload_bodies (
    hash        TEXT PRIMARY KEY,
    body        TEXT NOT NULL,
    first_seen  TEXT NOT NULL,
    bytes       INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS fetch_log (
    client_id   TEXT NOT NULL,
    started_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_raw_payloads_hash ON raw_payloads(body_hash);

CREATE INDEX IF NOT EXISTS ix_leads_client ON leads (client_id);
CREATE INDEX IF NOT EXISTS ix_leads_url ON leads (client_id, url_key);
CREATE INDEX IF NOT EXISTS ix_leads_name ON leads (client_id, name_key);
CREATE INDEX IF NOT EXISTS ix_messages_lead ON messages (lead_id);
CREATE INDEX IF NOT EXISTS ix_writelog_run ON write_log (run_id);
"""

def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")

def _now_utc():

    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

CLIENT_DB_DIR = "clients"

def path_for(client_id, base):

    if base and base.lower().endswith(".db"):
        return base
    return os.path.join(base or ".", CLIENT_DB_DIR, "%s.db" % client_id)

DIRECT_TABLES = ["clients", "campaigns", "senders", "leads", "messages",
                 "corrections", "sync_runs", "raw_payloads"]
INDIRECT_TABLES = [
    ("lead_campaign", "lead_id", "leads"),
    ("write_log", "run_id", "sync_runs"),
]

def split(shared_path, base, clients, log=None):

    log = log or (lambda msg: None)
    src = sqlite3.connect(shared_path)
    src.row_factory = sqlite3.Row
    report = {}

    for client_id in clients:
        target = path_for(client_id, base)
        if os.path.exists(target):
            raise ValueError("%s already exists; refusing to overwrite it" % target)
        out = Store(target)
        counts = {}

        for table in DIRECT_TABLES:
            column = "id" if table == "clients" else "client_id"
            rows = src.execute("SELECT * FROM %s WHERE %s = ?" % (table, column),
                               (client_id,)).fetchall()
            counts[table] = _copy(out, table, rows)

        for table, fk, parent in INDIRECT_TABLES:
            column = "id" if parent == "clients" else "client_id"
            rows = src.execute(
                "SELECT t.* FROM %s t JOIN %s p ON p.id = t.%s WHERE p.%s = ?"
                % (table, parent, fk, column), (client_id,)).fetchall()
            counts[table] = _copy(out, table, rows)

        out.db.commit()
        out.close()
        report[client_id] = counts
        log("%s -> %s" % (client_id, target))

    src.close()
    return report

def _copy(store, table, rows):
    if not rows:
        return 0
    columns = rows[0].keys()
    sql = "INSERT OR REPLACE INTO %s (%s) VALUES (%s)" % (
        table, ",".join(columns), ",".join("?" * len(columns)))
    store.db.executemany(sql, [tuple(r[c] for c in columns) for r in rows])
    return len(rows)

class Store(object):
    def __init__(self, path):
        self.path = path
        directory = os.path.dirname(os.path.abspath(path))
        if directory and not os.path.isdir(directory):
            os.makedirs(directory)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(SCHEMA)
        self._migrate()
        self.db.commit()

    MIGRATIONS = [
        ("leads", "url_key", "TEXT"),
        ("leads", "name_key", "TEXT"),
        ("lead_campaign", "observed_sent_at", "TEXT"),
        ("lead_campaign", "observed_accepted_at", "TEXT"),

        ("sync_runs", "summary", "TEXT"),
    ]

    def _migrate(self):
        for table, column, coltype in self.MIGRATIONS:
            existing = set(r["name"] for r in self.db.execute(
                "PRAGMA table_info(%s)" % table).fetchall())
            if not existing:
                continue
            if column not in existing:
                self.db.execute("ALTER TABLE %s ADD COLUMN %s %s"
                                % (table, column, coltype))

    def close(self):
        self.db.close()

    def upsert_client(self, client_id, name, workspace_id=None):
        self.db.execute(
            "INSERT INTO clients (id, name, workspace_id) VALUES (?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name, "
            "workspace_id=COALESCE(excluded.workspace_id, clients.workspace_id)",
            (client_id, name, workspace_id))
        self.db.commit()

    def upsert_campaign(self, client_id, heyreach_id, name=None, persona_tab=None):
        self.db.execute(
            "INSERT INTO campaigns (client_id, heyreach_campaign_id, name, persona_tab) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(client_id, heyreach_campaign_id) DO UPDATE SET "
            "name=COALESCE(excluded.name, campaigns.name), "
            "persona_tab=COALESCE(excluded.persona_tab, campaigns.persona_tab)",
            (client_id, str(heyreach_id), name, persona_tab))
        self.db.commit()
        row = self.db.execute(
            "SELECT id FROM campaigns WHERE client_id=? AND heyreach_campaign_id=?",
            (client_id, str(heyreach_id))).fetchone()
        return row["id"]

    def upsert_lead(self, client_id, lead_key, fields):

        now = _now()
        url_key = identity.normalise_url(fields.get("linkedin_url"))
        name_key = identity.name_key(fields.get("full_name"), fields.get("company"))
        self.db.execute(
            "INSERT INTO leads (client_id, lead_key, url_key, name_key, "
            "linkedin_url, full_name, "
            "company, title, email, location, first_seen, last_seen) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(client_id, lead_key) DO UPDATE SET "
            "url_key    =COALESCE(excluded.url_key,      leads.url_key), "
            "name_key   =COALESCE(excluded.name_key,     leads.name_key), "
            "linkedin_url=COALESCE(excluded.linkedin_url, leads.linkedin_url), "
            "full_name  =COALESCE(excluded.full_name,   leads.full_name), "
            "company    =COALESCE(excluded.company,     leads.company), "
            "title      =COALESCE(excluded.title,       leads.title), "
            "email      =COALESCE(excluded.email,       leads.email), "
            "location   =COALESCE(excluded.location,    leads.location), "
            "last_seen  =excluded.last_seen",
            (client_id, lead_key, url_key or None, name_key or None,
             fields.get("linkedin_url"), fields.get("full_name"),
             fields.get("company"), fields.get("title"),
             fields.get("email"), fields.get("location"), now, now))
        self.db.commit()
        row = self.db.execute(
            "SELECT id FROM leads WHERE client_id=? AND lead_key=?",
            (client_id, lead_key)).fetchone()
        return row["id"]

    _TRANSITIONS = {
        "ConnectionSent": ("observed_sent_at", "invite_sent_at", (None, "None")),
        "ConnectionAccepted": ("observed_accepted_at", "accepted_at",
                               (None, "None", "ConnectionSent")),
    }

    def observe_status(self, lead_id, campaign_id, new_status):

        rule = self._TRANSITIONS.get(new_status)
        if not rule:
            return None
        observed_col, real_col, from_states = rule

        row = self.db.execute(
            "SELECT status, %s AS real_v, %s AS obs_v FROM lead_campaign "
            "WHERE lead_id=? AND campaign_id=?" % (real_col, observed_col),
            (lead_id, campaign_id)).fetchone()
        if row is None:
            return None
        if row["status"] not in from_states:
            return None
        if row["real_v"] or row["obs_v"]:
            return None

        self.db.execute(
            "UPDATE lead_campaign SET %s=? WHERE lead_id=? AND campaign_id=?"
            % observed_col, (_now_utc(), lead_id, campaign_id))
        self.db.commit()
        return new_status

    def last_fetch_utc(self, client_id):

        row = self.db.execute("SELECT MAX(started_at) v FROM fetch_log WHERE client_id=?",
                              (client_id,)).fetchone()
        if row and row["v"]:
            return row["v"]
        row = self.db.execute(
            "SELECT MAX(fetched_at) v FROM raw_payloads WHERE client_id=? "
            "AND endpoint='/campaign/GetLeadsFromCampaign'", (client_id,)).fetchone()
        if not (row and row["v"]):
            return None
        epoch = time.mktime(time.strptime(row["v"][:19], "%Y-%m-%dT%H:%M:%S"))
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(epoch))

    def record_fetch(self, client_id, started_utc):
        self.db.execute("INSERT INTO fetch_log (client_id, started_at) VALUES (?,?)",
                        (client_id, started_utc))
        self.db.commit()

    def previous_status(self, lead_id, campaign_id):

        row = self.db.execute("SELECT status FROM lead_campaign WHERE lead_id=? AND campaign_id=?",
                              (lead_id, campaign_id)).fetchone()
        return "ABSENT" if row is None else row["status"]

    def date_invite_from_last_action(self, lead_id, campaign_id, last_action_at,
                                     observed_this_run=False):

        if not last_action_at:
            return False
        row = self.db.execute(
            "SELECT invite_sent_at, observed_sent_at FROM lead_campaign "
            "WHERE lead_id=? AND campaign_id=?", (lead_id, campaign_id)).fetchone()
        if row is None or row["invite_sent_at"]:
            return False
        if row["observed_sent_at"] and not observed_this_run:
            return False
        self.db.execute(
            "UPDATE lead_campaign SET invite_sent_at=? WHERE lead_id=? AND campaign_id=?",
            (last_action_at, lead_id, campaign_id))
        self.db.commit()
        return True

    def set_lead_campaign(self, lead_id, campaign_id, **stamps):
        self.db.execute(
            "INSERT INTO lead_campaign (lead_id, campaign_id, invite_sent_at, "
            "accepted_at, replied_at, status) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(lead_id, campaign_id) DO UPDATE SET "
            "invite_sent_at=COALESCE(excluded.invite_sent_at, lead_campaign.invite_sent_at), "
            "accepted_at   =COALESCE(excluded.accepted_at,    lead_campaign.accepted_at), "
            "replied_at    =COALESCE(excluded.replied_at,     lead_campaign.replied_at), "
            "status        =COALESCE(excluded.status,         lead_campaign.status)",
            (lead_id, campaign_id, stamps.get("invite_sent_at"),
             stamps.get("accepted_at"), stamps.get("replied_at"),
             stamps.get("status")))
        self.db.commit()

    def add_message(self, client_id, lead_id, campaign_id, sent_at, direction,
                    body, body_hash, step_label, label_source, label_detail):

        self.db.execute(
            "INSERT OR IGNORE INTO messages (client_id, lead_id, campaign_id, "
            "sent_at, direction, body, body_hash, step_label, label_source, "
            "label_detail) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (client_id, lead_id, campaign_id, sent_at, direction, body,
             body_hash, step_label, label_source, label_detail))
        self.db.commit()

    def record_correction(self, client_id, lead_id, field, machine_value, human_value):
        self.db.execute(
            "INSERT INTO corrections (client_id, lead_id, field, machine_value, "
            "human_value, seen_at) VALUES (?,?,?,?,?,?)",
            (client_id, lead_id, field, machine_value, human_value, _now()))
        self.db.commit()

    def save_raw(self, client_id, endpoint, request, body):

        text = json.dumps(body)
        digest = hashlib.sha1(text.encode("utf-8")).hexdigest()
        now = _now()
        self.db.execute(
            "INSERT OR IGNORE INTO payload_bodies (hash, body, first_seen, bytes) "
            "VALUES (?,?,?,?)", (digest, text, now, len(text)))
        self.db.execute(
            "INSERT INTO raw_payloads (client_id, endpoint, fetched_at, request, "
            "body, body_hash) VALUES (?,?,?,?,NULL,?)",
            (client_id, endpoint, now, json.dumps(request or {}), digest))
        self.db.commit()

    def raw_body(self, payload_id):

        row = self.db.execute(
            "SELECT COALESCE(b.body, r.body) FROM raw_payloads r "
            "LEFT JOIN payload_bodies b ON b.hash = r.body_hash WHERE r.id = ?",
            (payload_id,)).fetchone()
        return row[0] if row else None

    def start_run(self, client_id, mode):
        cur = self.db.execute(
            "INSERT INTO sync_runs (client_id, started_at, mode) VALUES (?,?,?)",
            (client_id, _now(), mode))
        self.db.commit()
        return cur.lastrowid

    def finish_run(self, run_id, status, cells_written=0, disagreements=0, detail=""):
        self.db.execute(
            "UPDATE sync_runs SET finished_at=?, status=?, cells_written=?, "
            "disagreements=?, detail=? WHERE id=?",
            (_now(), status, cells_written, disagreements, detail, run_id))
        self.db.commit()

    def record_summary(self, run_id, summary):

        import json
        try:
            self.db.execute("UPDATE sync_runs SET summary=? WHERE id=?",
                            (json.dumps(summary, ensure_ascii=False), run_id))
            self.db.commit()
        except Exception:
            pass

    def log_write(self, run_id, spreadsheet_id, tab, row, header, value, reason):

        self.db.execute(
            "INSERT INTO write_log (run_id, spreadsheet_id, tab, row, header, "
            "value, reason) VALUES (?,?,?,?,?,?,?)",
            (run_id, spreadsheet_id, tab, row, header, value, reason))
        self.db.commit()

        try:
            import csv
            log_dir = os.path.join(os.path.dirname(os.path.abspath(self.path)), "logs")
            if not os.path.isdir(log_dir):
                os.makedirs(log_dir)
            path = os.path.join(log_dir, "write-log.csv")
            new = not os.path.exists(path)
            with open(path, "a", newline="") as fh:
                writer = csv.writer(fh)
                if new:
                    writer.writerow(["written_at", "run_id", "spreadsheet_id",
                                     "tab", "row", "header", "value", "reason"])
                writer.writerow([_now(), run_id, spreadsheet_id, tab, row,
                                 header, value, reason])
        except Exception:

            self.db.execute(
                "UPDATE sync_runs SET detail = COALESCE(detail, '') || "
                "' [write-log mirror failed]' WHERE id = ?", (run_id,))
            self.db.commit()

    def leads_for_tab(self, client_id, persona_tab):

        return self.db.execute("""
            SELECT l.id, l.lead_key, l.linkedin_url, l.full_name, l.company,
                   l.title, l.email, l.location,
                   MIN(COALESCE(lc.invite_sent_at, lc.observed_sent_at)) AS invite_sent_at,
                   MIN(COALESCE(lc.accepted_at, lc.observed_accepted_at)) AS accepted_at,
                   MIN(lc.replied_at)     AS replied_at,
                   MAX(lc.status)         AS status,
                   GROUP_CONCAT(DISTINCT c.heyreach_campaign_id) AS campaign_ids
            FROM leads l
            JOIN lead_campaign lc ON lc.lead_id = l.id
            JOIN campaigns c      ON c.id = lc.campaign_id
            WHERE l.client_id = ? AND c.persona_tab = ?
            GROUP BY l.id
            ORDER BY l.id
        """, (client_id, persona_tab)).fetchall()

    def find_lead(self, client_id, url, name, company):

        url_key = identity.normalise_url(url)
        if url_key:
            row = self.db.execute(
                "SELECT id FROM leads WHERE client_id=? AND url_key=?",
                (client_id, url_key)).fetchone()
            if row:
                return row["id"], "url"
        key = identity.name_key(name, company)
        if key:
            rows = self.db.execute(
                "SELECT id FROM leads WHERE client_id=? AND name_key=?",
                (client_id, key)).fetchall()
            if len(rows) == 1:
                return rows[0]["id"], "name"
            if len(rows) > 1:
                return None, "ambiguous"
        return None, None

    def campaigns_of_lead(self, lead_id):

        return self.db.execute(
            "SELECT c.id, c.persona_tab FROM lead_campaign lc "
            "JOIN campaigns c ON c.id = lc.campaign_id "
            "WHERE lc.lead_id = ? ORDER BY c.id", (lead_id,)).fetchall()

    def outbound_for_relabel(self, client_id, only_unresolved=True):

        sql = ("SELECT m.id, m.body, m.step_label, m.label_source, "
               "c.persona_tab FROM messages m "
               "LEFT JOIN campaigns c ON c.id = m.campaign_id "
               "WHERE m.client_id = ? AND m.direction = 'out'")
        if only_unresolved:

            sql += " AND (m.step_label IS NULL OR m.label_source = 'closest')"
        return self.db.execute(sql, (client_id,)).fetchall()

    def set_message_label(self, message_id, label, source, detail):
        self.db.execute(
            "UPDATE messages SET step_label=?, label_source=?, label_detail=? "
            "WHERE id=?", (label, source, detail, message_id))
        self.db.commit()

    def messages_for_lead(self, lead_id):
        return self.db.execute(
            "SELECT sent_at, direction, step_label, label_source FROM messages "
            "WHERE lead_id=? AND direction='out' ORDER BY sent_at",
            (lead_id,)).fetchall()

    def campaign_contacted(self, client_id):

        rows = self.db.execute(
            "SELECT lc.lead_id FROM lead_campaign lc "
            "JOIN campaigns c ON c.id = lc.campaign_id "
            "WHERE c.client_id = ? AND lc.invite_sent_at IS NOT NULL "
            "UNION "
            "SELECT m.lead_id FROM messages m "
            "WHERE m.client_id = ? AND m.direction = 'out' "
            "AND m.campaign_id IS NOT NULL",
            (client_id, client_id)).fetchall()
        return set(r[0] for r in rows)

    def repliers(self, client_id):

        return self.db.execute("""
            SELECT l.id, l.lead_key, l.full_name, l.company, l.linkedin_url,
                   MIN(m.sent_at) AS first_reply_at,
                   MAX(m.sent_at) AS last_reply_at,
                   COUNT(*)       AS reply_count
            FROM messages m
            JOIN leads l ON l.id = m.lead_id
            WHERE m.client_id = ? AND m.direction = 'in'
            GROUP BY l.id
            ORDER BY first_reply_at DESC
        """, (client_id,)).fetchall()

    def thread_for_lead(self, lead_id):

        return self.db.execute(
            "SELECT sent_at, direction, body, step_label FROM messages "
            "WHERE lead_id=? ORDER BY sent_at", (lead_id,)).fetchall()

    def label_source_counts(self, client_id):
        rows = self.db.execute(
            "SELECT label_source, COUNT(*) n FROM messages WHERE client_id=? "
            "AND direction='out' GROUP BY label_source", (client_id,)).fetchall()
        return dict((r["label_source"] or "none", r["n"]) for r in rows)
