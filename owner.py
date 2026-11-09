"""Who owns this Mac, and where a second copy of a file might live.

Everything person- or country-specific lives here. Defaults come from the
system (your full name from `id -F`, your login, your region, mounted drives).
A JSON config adds what the system can't know, such as brands or a misspelling
of your name:

    {"names": ["Jane", "Doe"], "handles": ["janedoe"], "brands": ["AcmeApp"],
     "work_hints": ["acme"], "country": "US"}

"index_roots", when given, replaces the default list of places searched for
a second copy (home folders, iCloud Drive, every mounted drive).

Looked up at --config, then $DECLUTTER_CONFIG, then ~/.config/declutter/config.json.
"""
import json
import os
import re
import subprocess

STANDARD_ROOTS = ["~/Documents", "~/Desktop", "~/Movies", "~/Music", "~/Pictures",
                  "~/Library/Mobile Documents/com~apple~CloudDocs"]
SYSTEM_VOLUMES = re.compile(r"^(Recovery|Preboot|VM|Update|xarts|iSCPreboot|Hardware|com\.apple\..*)$")

# ID, tax and bank papers. "terms" are searched inside file text with Spotlight,
# "names" are matched against file names (regex fragments).
ID_PACKS = {
    "*": {
        "terms": ["Account Number", "Date of Birth"],
        "names": ["bank", "statement", "agreement", "rental", "lease", "visa", "passport", "invoice", "salary",
                  "payslip", "tax", "insurance", r"exit ?statement", "offer letter", "receipt", "kyc"],
    },
    "IN": {
        "terms": ["GSTIN", "Permanent Account Number", "IFSC", "Aadhaar", "Income Tax"],
        "names": ["gst", "gstr", r"itr\d?", r"inc[ -]?\d+", "pan", r"pans?card", r"aadhaa?r", "non judicial",
                  "stamp paper", r"form ?16", "phonepe", "sbi", "axis", "hdfc", "icici", "kotak"],
    },
    "US": {
        "terms": ["Social Security Number", "Employer Identification Number", "Form W-2", "Form 1099", "Form 1040"],
        "names": [r"w-?2", "1099", "1040", "ssn", "irs"],
    },
    "GB": {
        "terms": ["National Insurance Number", "Unique Taxpayer Reference", "HMRC"],
        "names": ["p60", "p45", "hmrc", "nino", "council tax"],
    },
}


def _run(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _country():
    loc = _run(["defaults", "read", "-g", "AppleLocale"])  # en_US@rg=inzzzz means region India
    m = re.search(r"rg=([a-z]{2})", loc) or re.search(r"_([A-Z]{2})", loc)
    return m.group(1).upper() if m else ""


def mounted_volumes():
    """Writable, non-system drives under /Volumes."""
    out = []
    for v in sorted(os.listdir("/Volumes")) if os.path.isdir("/Volumes") else []:
        p = os.path.join("/Volumes", v)
        if os.path.islink(p) or SYSTEM_VOLUMES.match(v) or not os.path.ismount(p):
            continue
        try:
            if os.statvfs(p).f_flag & os.ST_RDONLY:  # disk images and read-only shares
                continue
        except OSError:
            continue
        out.append(p)
    return out


def _alt(words):
    return "|".join(re.escape(w) for w in sorted(set(words), key=len, reverse=True) if w)


def _spaced(word):
    """AcmeApp -> acme ?app, so OCR text "Acme App" still matches."""
    parts = re.findall(r"[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])", word) or [word]
    return " ?".join(re.escape(p.lower()) for p in parts)


class Owner:
    def __init__(self, names, handles, brands, work_hints, country, index_roots, full_name=""):
        self.names = [n for n in names if len(n) >= 3]
        self.handles = [h for h in handles if h]
        self.brands = brands
        self.work_hints = work_hints
        self.country = country
        self.index_roots = index_roots
        self.full_name = full_name
        low = lambda xs: [x.lower() for x in xs]
        # File names: a name at a word start ("ann.gif", "Lease - Ann") but not inside "Annual".
        self.name_re = re.compile(r"(?<![a-z])(" + _alt(low(self.names + self.handles)) + ")", re.I) if self.names or self.handles else None
        self.hint_re = re.compile(r"(?<![a-z])(" + _alt(low(work_hints + brands)) + ")", re.I) if work_hints or brands else None
        self.brand_re = re.compile("|".join(_spaced(b) for b in brands), re.I) if brands else None
        # Text: whole words only, and squashed forms that survive PDF text extraction losing spaces.
        self.word_re = re.compile(r"\b(" + _alt(low(self.names + self.handles + brands)) + r")\b", re.I) \
            if self.names or self.handles or brands else None
        squash = lambda s: re.sub(r"\W+", "", s).lower()
        self.squashed = sorted({squash(full_name)} | {squash(h) for h in self.handles} | {squash(b) for b in brands} - {""})
        pack = [ID_PACKS["*"]] + ([ID_PACKS[country]] if country in ID_PACKS else [])
        self.id_terms = [t for p in pack for t in p["terms"]]
        self.id_name_re = re.compile(r"(?<![a-z])(" + "|".join(n for p in pack for n in p["names"]) + r")(?![a-z])", re.I)

    @property
    def spotlight_terms(self):
        return sorted(set(self.names + self.handles + self.brands))

    def in_name(self, name):
        return bool(self.name_re and self.name_re.search(name)) or bool(self.hint_re and self.hint_re.search(name))

    def in_text(self, text):
        """Your name, handle or brand in free text (PDF text, metadata, OCR)."""
        flat = re.sub(r"\W+", "", text).lower()
        return bool(self.word_re and self.word_re.search(text)) or any(s in flat for s in self.squashed) \
            or bool(self.brand_re and self.brand_re.search(text))


def config_path(explicit=None):
    for p in (explicit, os.environ.get("DECLUTTER_CONFIG"), "~/.config/declutter/config.json"):
        if p and os.path.exists(os.path.expanduser(p)):
            return os.path.expanduser(p)
    return None


def load(explicit=None):
    cfg = {}
    path = config_path(explicit)
    if path:
        with open(path) as f:
            cfg = json.load(f)
    full = cfg.get("full_name") or _run(["id", "-F"])
    names = cfg.get("names") or full.split()
    handles = cfg.get("handles") or [os.environ.get("USER", "")]
    if "index_roots" in cfg:  # an explicit list replaces the defaults
        roots = [os.path.expanduser(r) for r in cfg["index_roots"]]
    else:
        roots = [os.path.expanduser(r) for r in STANDARD_ROOTS] + mounted_volumes()
    return Owner(names=names, handles=handles, brands=cfg.get("brands", []), work_hints=cfg.get("work_hints", []),
                 country=(cfg.get("country") or _country()).upper(), index_roots=roots, full_name=full)
