from __future__ import unicode_literals

import io
import json
import os

from . import diff

class ConfigError(Exception):
    pass

class TabConfig(object):

    def __init__(self, name, data):
        self.name = name
        self.campaign_ids = [str(c) for c in data.get("campaign_ids", [])]
        self.historical_campaign_ids = [
            str(c) for c in data.get("historical_campaign_ids", [])]
        for c in self.historical_campaign_ids:
            if c not in self.campaign_ids:
                self.campaign_ids.append(c)
        if not self.campaign_ids:
            raise ConfigError("tab %r has no campaign_ids" % name)

        self.live_campaign_ids = [c for c in self.campaign_ids
                                  if c not in self.historical_campaign_ids]

class ClientConfig(object):
    def __init__(self, data):
        self.id = data["id"]
        self.name = data.get("name", self.id)
        self.workspace_id = data.get("workspace_id")

        self.api_key_env = data.get(
            "api_key_env", "HEYREACH_API_KEY_" + self.id.upper())
        self.spreadsheet_id = data["spreadsheet_id"]
        self.header_row = int(data.get("header_row", 2))
        self.first_data_row = int(data.get("first_data_row", 4))
        self.drips_tab = data.get("drips_tab", "Drips")
        self.sender_ids = [str(s) for s in data.get("sender_ids", [])]

        self.staging = data.get("staging") or None
        self.staging_retired_on = (self.staging or {}).get("retired_on") or None
        self.tabs = [TabConfig(n, d) for n, d in sorted(data.get("tabs", {}).items())]

        self.columns = dict(data.get("columns", {}))

        self.li_bands = int(data.get("li_bands", 7))

        self.ownership = {}
        for header, own in data.get("ownership", {}).items():
            if own not in (diff.MACHINE, diff.SHARED, diff.HUMAN, diff.REPORT,
                           diff.FILL_ONLY, diff.CONVENTION):
                raise ConfigError(
                    "ownership for %r must be machine, shared, fill, convention, "
                    "report or human, got %r" % (header, own))
            if "{n}" in header:
                for band in range(1, self.li_bands + 1):
                    self.ownership[header.replace("{n}", str(band))] = own
            else:
                self.ownership[header] = own

        self.timezone_offset_minutes = int(data.get("timezone_offset_minutes", 0))

        self.date_format = data.get("date_format", "%d %b %y")
        self.date_strip_leading_zero = bool(
            data.get("date_strip_leading_zero", True))

        self.max_cells_per_run = int(data.get("max_cells_per_run", 300))

        self.max_new_rows_per_run = int(data.get("max_new_rows_per_run", 60))

        self.reply_blocks = dict(data.get("reply_blocks", {}))

        self.mirror = dict(data.get("mirror", {}))

        self.report_tab = data.get("report_tab", "")

        self.reply_done_requires_match = bool(
            data.get("reply_done_requires_match", False))

        if self.first_data_row <= self.header_row:
            raise ConfigError("first_data_row must be below header_row")
        if not self.tabs:
            raise ConfigError("client %r has no tabs" % self.id)

    def header(self, logical):
        try:
            return self.columns[logical]
        except KeyError:
            raise ConfigError(
                "no header mapped for %r on client %r" % (logical, self.id))

    def li_header(self, band, part):

        key = "li_type" if part == "type" else "li_date"
        return self.header(key).replace("{n}", str(band))

    def ownership_of(self, header):
        return self.ownership.get(header, diff.HUMAN)

def load_client(path):
    with io.open(path, "r", encoding="utf-8") as fh:
        return ClientConfig(json.load(fh))

def load_all(directory):
    out = {}
    for name in sorted(os.listdir(directory)):
        if not name.endswith(".json"):
            continue
        cfg = load_client(os.path.join(directory, name))
        out[cfg.id] = cfg
    return out

def allowlist(configs):

    return set(c.spreadsheet_id for c in configs.values())
