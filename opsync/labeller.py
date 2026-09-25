from __future__ import unicode_literals

import re
import unicodedata

FLOOR = 0.45
MARGIN = 0.08

_WORD = re.compile(r"[a-z0-9']+")

_STOPWORDS = frozenset("""
a an and are as at be but by for from had has have he her hey hi his i if in is
it its me my of on or our so that the their them they this to was we were will
with you your
""".split())

def tokens(text):
    if not text:
        return []
    s = unicodedata.normalize("NFKD", str(text)).lower()
    s = "".join(c for c in s if not unicodedata.combining(c))
    return [w for w in _WORD.findall(s) if w not in _STOPWORDS and len(w) > 1]

def score(sent, template):

    a, b = set(tokens(sent)), set(tokens(template))
    if not a or not b:
        return 0.0
    shared = len(a & b)
    return (shared / float(len(a)) + shared / float(len(b))) / 2.0

class Template(object):
    def __init__(self, persona, band, variant, text):
        self.persona = persona
        self.band = band
        self.variant = variant
        self.text = text

    @property
    def label(self):
        return "LI %d %s" % (self.band, self.variant)

    def __repr__(self):
        return "<Template %s %s>" % (self.persona, self.label)

def label_message(sent_text, templates, api_step_label=None):

    if api_step_label:
        return api_step_label, "api", "step supplied by HeyReach"

    if not sent_text or not templates:
        return None, "unresolved", "no text or no templates"

    scored = sorted(
        ((score(sent_text, t.text), t) for t in templates),
        key=lambda pair: pair[0],
        reverse=True,
    )
    best_score, best = scored[0]
    if best_score < FLOOR:
        return None, "unresolved", "best score %.2f below floor %.2f" % (best_score, FLOOR)
    if len(scored) > 1:
        second_score = scored[1][0]
        if best_score - second_score < MARGIN:

            if scored[1][1].label == best.label:
                return best.label, "drips", "score %.2f" % best_score
            return best.label, "closest", "closest of two: %s %.2f over %s %.2f" % (
                best.label, best_score, scored[1][1].label, second_score)
    return best.label, "drips", "score %.2f" % best_score

MIN_TEMPLATE_CHARS = 40

def parse_drips(rows):

    band_re = re.compile(r"^LI\s*([1-7])$", re.I)

    type_re = re.compile(r"^t(?:ype|y)\s*([A-Z])\s*(?:$|[-–—:(,.])", re.I)

    grid = [[("" if c is None else str(c)).strip() for c in row] for row in rows]
    width = max([len(r) for r in grid] or [0])

    def at(r, c):
        if 0 <= r < len(grid) and 0 <= c < len(grid[r]):
            return grid[r][c]
        return ""

    templates = []
    for col in range(width):
        persona = None
        band = None
        for row in range(len(grid)):
            value = at(row, col)
            if not value:
                continue

            m = band_re.match(value)
            if m:
                band = int(m.group(1))
                continue

            m = type_re.match(value)
            if m:
                text = at(row + 1, col)

                if band_re.match(text) or type_re.match(text):
                    continue
                if band and len(text) > MIN_TEMPLATE_CHARS:
                    templates.append(
                        Template(persona, band, m.group(1).upper(), text))
                continue

            if value.lower().startswith("http"):
                continue

            if re.match(r"^t(?:ype|y)\s*[A-Z]", value, re.I):
                continue
            if len(value) < 60 and not value[0].isdigit():
                persona = value
                band = None

    return templates
