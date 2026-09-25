from __future__ import unicode_literals

import json
import time

try:
    from urllib.request import Request, urlopen
    from urllib.error import HTTPError, URLError
except ImportError:
    from urllib2 import Request, urlopen, HTTPError, URLError

BASE = "https://api.heyreach.io/api/public"

MAX_PER_MINUTE = 240

class HeyReachError(Exception):
    pass

class RateLimiter(object):
    def __init__(self, per_minute=MAX_PER_MINUTE):
        self.interval = 60.0 / float(per_minute)
        self._last = 0.0

    def wait(self):
        gap = time.time() - self._last
        if gap < self.interval:
            time.sleep(self.interval - gap)
        self._last = time.time()

class Client(object):
    def __init__(self, api_key, store=None, client_id=None, base=BASE):
        if not api_key:
            raise HeyReachError("no API key supplied")
        self.api_key = api_key
        self.base = base
        self.store = store
        self.client_id = client_id
        self.limiter = RateLimiter()

    def _call(self, method, path, body=None, retries=4):
        url = self.base.rstrip("/") + "/" + path.lstrip("/")
        payload = None if body is None else json.dumps(body).encode("utf-8")
        attempt = 0
        while True:
            attempt += 1
            self.limiter.wait()
            req = Request(url, data=payload, method=method)
            req.add_header("X-API-KEY", self.api_key)
            req.add_header("Accept", "application/json")
            if payload is not None:
                req.add_header("Content-Type", "application/json")
            try:
                resp = urlopen(req, timeout=60)
                raw = resp.read().decode("utf-8")
                data = json.loads(raw) if raw.strip() else {}
                if self.store and self.client_id:
                    self.store.save_raw(self.client_id, path, body, data)
                return data
            except HTTPError as err:

                if err.code in (429, 500, 502, 503, 504) and attempt <= retries:
                    time.sleep(min(60, 2 ** attempt))
                    continue
                detail = ""
                try:
                    detail = err.read().decode("utf-8")[:400]
                except Exception:
                    pass
                raise HeyReachError("%s %s returned %s %s"
                                    % (method, path, err.code, detail))
            except URLError as err:
                if attempt <= retries:
                    time.sleep(min(60, 2 ** attempt))
                    continue
                raise HeyReachError("%s %s failed: %s" % (method, path, err))

    def check_key(self):

        self._call("GET", "/auth/CheckApiKey")
        return True

    def _paged(self, path, body=None, page_size=100, limit_key="limit",
               offset_key="offset"):

        offset = 0
        while True:
            page_body = dict(body or {})
            page_body[offset_key] = offset
            page_body[limit_key] = page_size
            data = self._call("POST", path, page_body)

            if isinstance(data, list):
                items = data
            elif isinstance(data, dict):
                items = data.get("items")
                if items is None:
                    items = data.get("data")
                if items is None:
                    for value in data.values():
                        if isinstance(value, list):
                            items = value
                            break
            else:
                items = None

            if items is None:
                raise HeyReachError(
                    "%s returned an unexpected shape, keys: %s"
                    % (path, sorted(data.keys()) if isinstance(data, dict) else type(data)))

            for item in items:
                yield item
            if len(items) < page_size:
                return
            offset += page_size

    def campaigns(self):
        return self._paged("/campaign/GetAll")

    def leads_in_campaign(self, campaign_id):
        return self._paged("/campaign/GetLeadsFromCampaign",
                           {"campaignId": campaign_id})

    def conversations(self, campaign_id=None):

        body = {}
        if campaign_id is not None:
            body["campaignIds"] = [campaign_id]
        return self._paged("/inbox/GetConversationsV2", body)

    def linkedin_accounts(self):
        return self._paged("/li_account/GetAll")

    def discover(self, campaign_id=None, sample=2):

        report = {}
        probes = [("campaigns", lambda: self.campaigns()),
                  ("linkedin_accounts", lambda: self.linkedin_accounts())]
        if campaign_id is not None:
            probes.append(("leads_in_campaign",
                           lambda: self.leads_in_campaign(campaign_id)))
            probes.append(("conversations",
                           lambda: self.conversations(campaign_id)))

        for name, call in probes:
            try:
                rows = []
                for i, item in enumerate(call()):
                    rows.append(item)
                    if i + 1 >= sample:
                        break
                report[name] = {
                    "ok": True,
                    "count_sampled": len(rows),
                    "keys": sorted(rows[0].keys()) if rows and isinstance(rows[0], dict) else [],
                    "first": rows[0] if rows else None,
                }
            except HeyReachError as err:
                report[name] = {"ok": False, "error": str(err)}
        return report
