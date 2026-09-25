from __future__ import unicode_literals

HEADING_SUFFIX = "Replies"

def _letters(index_zero_based):
    s = ""
    n = index_zero_based + 1
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s

MARGIN = 3

GROW_BY = 25

MAX_ROWS_PER_RUN = 200

class Block(object):

    def __init__(self, heading, row, column, first_row, capacity, used,
                 first_value=""):
        self.heading = heading
        self.heading_row = row
        self.column = column
        self.first_row = first_row
        self.capacity = capacity
        self.used = used
        self.first_value = first_value

    @property
    def free(self):
        return max(0, self.capacity - self.used)

    @property
    def broken(self):

        return self.first_value.startswith("#")

    @property
    def label(self):

        return "%s %s" % (_letters(self.column), self.heading)

    def __repr__(self):
        return "<Block %s row %d %d/%d>" % (
            self.label, self.heading_row, self.used, self.capacity)

def _text(values, row, column):
    try:
        return str(values[row - 1][column] or "").strip()
    except (IndexError, TypeError):
        return ""

def map_blocks(values, total_rows=None):

    if total_rows is None:
        total_rows = max(len(values), 1)

    heads = []
    for row_index, row in enumerate(values, start=1):
        for col_index, cell in enumerate(row or []):
            text = str(cell or "").strip()
            if text.endswith(HEADING_SUFFIX) and len(text) > len(HEADING_SUFFIX):
                heads.append((row_index, col_index, text))

    by_column = {}
    for row, column, text in heads:
        by_column.setdefault(column, []).append((row, text))

    blocks = []
    for column, items in sorted(by_column.items()):
        items.sort()
        for position, (row, text) in enumerate(items):

            first_row = row + 2
            if position + 1 < len(items):

                last_row = items[position + 1][0] - 1
            else:

                last_row = total_rows
            capacity = max(0, last_row - first_row + 1)
            data_column = column + 1
            used = 0
            for r in range(first_row, last_row + 1):
                if _text(values, r, data_column):
                    used = r - first_row + 1
            blocks.append(Block(text, row, data_column, first_row, capacity,
                                used, _text(values, first_row, data_column)))
    return blocks

def bands(blocks):

    out = {}
    for block in blocks:
        out.setdefault(block.first_row, []).append(block)
    return out

def growth_plan(blocks, margin=MARGIN, grow_by=GROW_BY):

    plan = []
    for first_row, group in sorted(bands(blocks).items()):
        need = max(b.used for b in group) + margin
        capacity = min(b.capacity for b in group)

        collapsed = [b for b in group if b.broken]
        if collapsed:
            plan.append({
                "before_row": first_row + capacity,
                "rows": grow_by,
                "why": "%s has collapsed into #REF!, so its real size cannot be "
                       "read from this tab. Growing by %d and re-measuring"
                       % (collapsed[0].label, grow_by),
                "blocks": [b.label for b in group],
                "collapsed": True,
            })
            continue

        if need <= capacity:
            continue
        short = need - capacity
        add = max(grow_by, short)
        tightest = max(group, key=lambda b: b.used)
        plan.append({

            "before_row": first_row + capacity,
            "rows": add,
            "why": "%s needs %d rows and has %d"
                   % (tightest.label, need, capacity),
            "blocks": [b.label for b in group],
            "collapsed": False,
        })
    return plan

def check(plan, max_rows=MAX_ROWS_PER_RUN):

    problems = []
    total = sum(step["rows"] for step in plan)
    if total > max_rows:
        problems.append(
            "growth plan inserts %d rows, cap is %d. Either a lot of replies "
            "arrived at once or the counting is wrong, and both deserve a look "
            "before the tab is reshaped" % (total, max_rows))
    for step in plan:
        if step["rows"] <= 0 or step["before_row"] < 2:
            problems.append("nonsensical step %r" % (step,))
    return problems

def room_for(blocks, heading_word, needed=1, margin=0):

    word = heading_word.split()[0].lower()
    hits = [b for b in blocks if b.heading.lower().startswith(word)]
    if not hits:
        return True, None
    tightest = min(hits, key=lambda b: b.free)
    return tightest.free >= needed + margin, tightest

def numbering_plan(blocks, values, grow_by=GROW_BY):

    out = []
    for block in blocks:
        column = block.column - 1
        depth = min(block.capacity, max(block.used, 1) + grow_by)
        for offset in range(depth):
            row = block.first_row + offset
            current = _text(values, row, column)
            if current and not current.replace(".", "", 1).isdigit():
                continue
            data = "%s%d" % (_letters(block.column), row)
            if offset == 0:
                formula = '=IF(%s="","",1)' % data
            else:
                formula = '=IF(%s="","",%s%d+1)' % (
                    data, _letters(column), row - 1)
            out.append((row, column, formula))
    return out

def summary_rows(blocks):

    rows = []
    for block in sorted(blocks, key=lambda b: (b.free, b.heading)):
        state = "BROKEN" if block.broken else ("%d free" % block.free)
        rows.append([block.label, "row %d" % block.first_row,
                     "%d of %d" % (block.used, block.capacity), state])
    return rows
