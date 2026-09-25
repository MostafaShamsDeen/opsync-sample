from __future__ import unicode_literals

HTTP_TIMEOUT_SECONDS = 180
API_RETRIES = 4

RETRY_STATUSES = (408, 409)
RETRY_EXTRA = 4
RETRY_BACKOFF = 1.5

def execute(request):

    import random
    import time

    from googleapiclient.errors import HttpError
    for attempt in range(RETRY_EXTRA + 1):
        try:
            return request.execute(num_retries=API_RETRIES)
        except HttpError as err:
            status = getattr(getattr(err, "resp", None), "status", None)
            if status not in RETRY_STATUSES or attempt == RETRY_EXTRA:
                raise

            time.sleep(RETRY_BACKOFF * (2 ** attempt) * (0.5 + random.random()))

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
READ_SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]

class SheetError(Exception):
    pass

def _column_letter(index_one_based):
    letters = ""
    n = index_one_based
    while n > 0:
        n, rem = divmod(n - 1, 26)
        letters = chr(65 + rem) + letters
    return letters

class Tab(object):

    def __init__(self, name, values, header_row, first_data_row, band_row=None):
        self.name = name
        self.values = values
        self.header_row = header_row
        self.first_data_row = first_data_row
        self.band_row = band_row if band_row is not None else max(1, header_row - 1)

        header_index = header_row - 1
        if header_index >= len(values):
            raise SheetError("tab %r has no row %d to read headers from"
                             % (name, header_row))

        subs = [(h or "").strip() for h in values[header_index]]

        bands = []
        current = ""
        band_index = self.band_row - 1
        band_values = values[band_index] if band_index < len(values) else []
        for i in range(len(subs)):
            raw = (band_values[i] or "").strip() if i < len(band_values) else ""
            if raw:
                current = raw
            bands.append(current)

        self.headers = []
        counts = {}
        candidates = []
        for i, sub in enumerate(subs):
            band = bands[i]
            names = []
            if sub:
                names.append(sub)
                if band:
                    names.append(band + " / " + sub)
            elif band:
                names.append(band)
            candidates.append(names)
            for n in names:
                counts[n] = counts.get(n, 0) + 1
            self.headers.append(names[-1] if names else "")

        self._by_header = {}
        for i, names in enumerate(candidates):
            for n in names:
                if counts[n] == 1:
                    self._by_header[n] = i
        self.ambiguous = sorted(n for n, c in counts.items() if c > 1)

        self.names = set(self._by_header)

    def has(self, header):
        return header in self._by_header

    def assert_headers(self, required):
        missing = [h for h in required if h not in self._by_header]
        if missing:
            raise SheetError(
                "tab %r is missing expected headers: %s. Refusing to write into "
                "a sheet whose shape has changed." % (self.name, ", ".join(missing)))

    def column_index(self, header):
        try:
            return self._by_header[header]
        except KeyError:
            raise SheetError("tab %r has no column headed %r" % (self.name, header))

    def rows(self):

        for offset in range(self.first_data_row - 1, len(self.values)):
            raw = self.values[offset]
            if not any((c or "").strip() for c in raw):
                continue
            record = {}
            for header, index in self._by_header.items():
                record[header] = raw[index] if index < len(raw) else ""
            yield offset + 1, record

    def a1(self, row, header):
        return "%s!%s%d" % (self.name, _column_letter(self.column_index(header) + 1), row)

def tab_from_rows(name, values, header_row=2, first_data_row=4, band_row=None):

    return Tab(name, values, header_row, first_data_row, band_row)

class OfflineSheets(object):

    read_only = True

    def __init__(self, path):
        import io
        import json
        with io.open(path, "r", encoding="utf-8-sig") as fh:
            self.snapshot = json.load(fh)
        self.source = self.snapshot.get("source", "undated snapshot")

    def read_tab(self, spreadsheet_id, tab_name, header_row=2, first_data_row=4):
        tabs = self.snapshot.get("tabs", {})
        if tab_name not in tabs:
            raise SheetError("snapshot has no tab %r. It holds: %s"
                             % (tab_name, ", ".join(sorted(tabs))))
        return Tab(tab_name, tabs[tab_name]["values"], header_row, first_data_row)

    def apply(self, *args, **kwargs):
        raise SheetError(
            "this is a snapshot, not a spreadsheet. Writing needs Google "
            "credentials and a live read, so that a write is never based on "
            "data that may have moved on.")

class SheetsClient(object):

    def __init__(self, credentials_path, read_only=True):
        try:
            from google.oauth2 import service_account
            from googleapiclient.discovery import build
        except ImportError:
            raise SheetError(
                "Google client libraries are not installed. Run:\n"
                "  py -m pip install google-api-python-client google-auth\n"
                "Everything except reading and writing sheets works without them.")
        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        creds = service_account.Credentials.from_service_account_file(
            credentials_path, scopes=READ_SCOPES if read_only else SCOPES)

        api_kwargs = {"credentials": creds}
        try:
            import httplib2
            import google_auth_httplib2
            api_kwargs = {"http": google_auth_httplib2.AuthorizedHttp(
                creds, http=httplib2.Http(timeout=HTTP_TIMEOUT_SECONDS))}
        except ImportError:
            pass
        self.api = build("sheets", "v4", cache_discovery=False, **api_kwargs)
        self.read_only = read_only

    def read_tab(self, spreadsheet_id, tab_name, header_row=2, first_data_row=4):
        result = execute(self.api.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range="'%s'" % tab_name,
            valueRenderOption="FORMATTED_VALUE"))
        return Tab(tab_name, result.get("values", []), header_row, first_data_row)

    def ensure_tab(self, spreadsheet_id, tab_name, hidden=True,
                   rows=60, columns=6):

        meta = execute(self.api.spreadsheets().get(spreadsheetId=spreadsheet_id))
        for sheet in meta.get("sheets", []):
            props = sheet.get("properties", {})
            if props.get("title") == tab_name:
                return props.get("sheetId"), False

        if self.read_only:
            raise SheetError("this client was opened read only")
        result = execute(self.api.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": [{"addSheet": {"properties": {
                "title": tab_name, "hidden": hidden,
                "gridProperties": {"rowCount": rows,
                                   "columnCount": columns}}}}]}))
        return result["replies"][0]["addSheet"]["properties"]["sheetId"], True

    def ensure_grid_size(self, spreadsheet_id, tab_name, rows=0, columns=0):

        meta = execute(self.api.spreadsheets().get(spreadsheetId=spreadsheet_id))
        for sheet in meta.get("sheets", []):
            props = sheet.get("properties", {})
            if props.get("title") != tab_name:
                continue
            grid = props.get("gridProperties", {})
            want_rows = max(rows, grid.get("rowCount", 0))
            want_cols = max(columns, grid.get("columnCount", 0))
            if (want_rows, want_cols) == (grid.get("rowCount"),
                                          grid.get("columnCount")):
                return props.get("sheetId"), False
            if self.read_only:
                raise SheetError("this client was opened read only")
            execute(self.api.spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={"requests": [{"updateSheetProperties": {
                    "properties": {"sheetId": props["sheetId"],
                                   "gridProperties": {"rowCount": want_rows,
                                                      "columnCount": want_cols}},
                    "fields": "gridProperties(rowCount,columnCount)"}}]}))
            return props.get("sheetId"), True
        return None, False

    def row_count(self, spreadsheet_id, tab_name):

        meta = execute(self.api.spreadsheets().get(spreadsheetId=spreadsheet_id))
        for sheet in meta.get("sheets", []):
            props = sheet.get("properties", {})
            if props.get("title") == tab_name:
                return props.get("gridProperties", {}).get("rowCount")
        return None

    def sheet_id_of(self, spreadsheet_id, tab_name):
        meta = execute(self.api.spreadsheets().get(spreadsheetId=spreadsheet_id))
        for sheet in meta.get("sheets", []):
            props = sheet.get("properties", {})
            if props.get("title") == tab_name:
                return props.get("sheetId")
        return None

    def insert_rows(self, spreadsheet_id, sheet_id, before_row, count):

        if self.read_only:
            raise SheetError("this client was opened read only")
        execute(self.api.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": [{"insertDimension": {
                "range": {"sheetId": sheet_id, "dimension": "ROWS",
                          "startIndex": before_row - 1,
                          "endIndex": before_row - 1 + count},
                "inheritFromBefore": True}}]}))
        return count

    def delete_rows(self, spreadsheet_id, sheet_id, rows):

        if self.read_only:
            raise SheetError("this client was opened read only")
        if not rows:
            return 0
        requests = []
        for row in sorted(set(rows), reverse=True):
            requests.append({"deleteDimension": {"range": {
                "sheetId": sheet_id, "dimension": "ROWS",
                "startIndex": row - 1, "endIndex": row}}})
        execute(self.api.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": requests}))
        return len(requests)

    def read_grid(self, spreadsheet_id, tab_name):

        try:
            result = execute(self.api.spreadsheets().values().get(
                spreadsheetId=spreadsheet_id,
                range="'%s'" % tab_name,
                valueRenderOption="FORMATTED_VALUE"))
        except Exception as err:
            if "Unable to parse range" in str(err):
                return None
            raise
        return result.get("values", [])

    def update_scattered(self, spreadsheet_id, tab_name, cells):

        if self.read_only:
            raise SheetError("this client was opened read only")
        if not cells:
            return 0
        data = [{"range": "'%s'!%s%d" % (tab_name, _column_letter(col + 1), row),
                 "values": [[value]]} for row, col, value in cells]
        execute(self.api.spreadsheets().values().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"valueInputOption": "RAW", "data": data}))
        return len(data)

    def update_scattered_formulas(self, spreadsheet_id, tab_name, cells):

        if self.read_only:
            raise SheetError("this client was opened read only")
        if not cells:
            return 0
        data = [{"range": "'%s'!%s%d" % (tab_name, _column_letter(col + 1), row),
                 "values": [[value]]} for row, col, value in cells]
        execute(self.api.spreadsheets().values().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"valueInputOption": "USER_ENTERED", "data": data}))
        return len(data)

    def append_rows(self, spreadsheet_id, tab_name, rows):
        if self.read_only:
            raise SheetError("this client was opened read only")
        if not rows:
            return 0
        execute(self.api.spreadsheets().values().append(
            spreadsheetId=spreadsheet_id,
            range="'%s'!A1" % tab_name,
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": rows}))
        return len(rows)

    def format_queue_tab(self, spreadsheet_id, sheet_id, columns, choice_column,
                         choices, last_row, layout=None, done_column=None,
                         done_value="yes"):

        if self.read_only:
            raise SheetError("this client was opened read only")
        index_of = {}
        if layout:
            for key, header, pixels in columns:
                if header in layout:
                    index_of[key] = layout[header]
        width = max(index_of.values()) + 1 if index_of else len(columns)
        requests = [
            {"updateSheetProperties": {
                "properties": {"sheetId": sheet_id,
                               "gridProperties": {"frozenRowCount": 1}},
                "fields": "gridProperties.frozenRowCount"}},
            {"repeatCell": {
                "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1},
                "cell": {"userEnteredFormat": {
                    "textFormat": {"bold": True},
                    "backgroundColor": {"red": 0.92, "green": 0.92, "blue": 0.92},
                    "verticalAlignment": "MIDDLE"}},
                "fields": "userEnteredFormat(textFormat,backgroundColor,verticalAlignment)"}},
            {"setDataValidation": {
                "range": {"sheetId": sheet_id, "startRowIndex": 1,
                          "endRowIndex": max(last_row, 2),
                          "startColumnIndex": choice_column,
                          "endColumnIndex": choice_column + 1},
                "rule": {
                    "condition": {"type": "ONE_OF_LIST",
                                  "values": [{"userEnteredValue": c} for c in choices]},
                    "strict": True, "showCustomUi": True}}},
        ]
        for position, (key, _, pixels) in enumerate(columns):
            index = index_of.get(key, position)
            requests.append({"updateDimensionProperties": {
                "range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                          "startIndex": index, "endIndex": index + 1},
                "properties": {"pixelSize": pixels},
                "fields": "pixelSize"}})

        reply_index = [index_of.get(k, i) for i, (k, _, _) in enumerate(columns)
                       if k == "reply"]
        for index in range(width):
            strategy = "WRAP" if index in reply_index else "CLIP"
            requests.append({"repeatCell": {
                "range": {"sheetId": sheet_id, "startRowIndex": 1,
                          "startColumnIndex": index, "endColumnIndex": index + 1},
                "cell": {"userEnteredFormat": {"wrapStrategy": strategy,
                                               "verticalAlignment": "TOP"}},
                "fields": "userEnteredFormat(wrapStrategy,verticalAlignment)"}})

        if done_column is not None:

            requests.append({"clearBasicFilter": {"sheetId": sheet_id}})
            requests.append({"setBasicFilter": {"filter": {
                "range": {"sheetId": sheet_id, "startRowIndex": 0,
                          "endRowIndex": max(last_row, 2),
                          "startColumnIndex": 0, "endColumnIndex": width},
                "criteria": {str(done_column): {"hiddenValues": [done_value]}}}}})

        execute(self.api.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id, body={"requests": requests}))

    def write_panel(self, spreadsheet_id, tab_name, rows):

        if self.read_only:
            raise SheetError("this client was opened read only")
        execute(self.api.spreadsheets().values().clear(
            spreadsheetId=spreadsheet_id, range="'%s'" % tab_name, body={}))
        execute(self.api.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range="'%s'!A1" % tab_name,
            valueInputOption="RAW",
            body={"values": rows}))

    def append_rows_for_leads(self, spreadsheet_id, tab, rows, run_id=None,
                              store=None):

        if self.read_only:
            raise SheetError("this client was opened read only")
        if not rows:
            return 0

        data = []
        for row_number, values in rows:
            for header, value in values.items():
                data.append({"range": tab.a1(row_number, header),
                             "values": [[value]]})
        if not data:
            return 0

        execute(self.api.spreadsheets().values().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"valueInputOption": "USER_ENTERED", "data": data}))

        if store and run_id:
            for row_number, values in rows:
                for header, value in values.items():
                    store.log_write(run_id, spreadsheet_id, tab.name,
                                    row_number, header, value, "new row")
        return len(rows)

    def apply(self, spreadsheet_id, tab, fills, run_id=None, store=None):

        if self.read_only:
            raise SheetError("this client was opened read only")
        if not fills:
            return 0

        data = []
        for change in fills:
            data.append({
                "range": tab.a1(change.row, change.header),
                "values": [[change.desired]],
            })

        execute(self.api.spreadsheets().values().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"valueInputOption": "USER_ENTERED", "data": data}))

        if store and run_id:
            for change in fills:
                store.log_write(run_id, spreadsheet_id, tab.name, change.row,
                                change.header, change.desired, "fill")
        return len(data)
