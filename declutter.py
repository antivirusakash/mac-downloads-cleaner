#!/usr/bin/env python3
"""declutter: find files you probably don't need. Preview only, never deletes.

Usage: python3 -I declutter.py [--out DIR] ROOT [ROOT ...]
Writes DIR/report.json (every flagged item with its reason).

Each item gets a category, a confidence tier, and a reason:
  safe    exact copy or rebuildable (duplicates, build folders of idle projects,
          zips whose extracted folder matches, installers for installed apps)
  likely  old disposable stuff (old screenshots, installers, big logs)
  review  big and untouched for a long time; only you can judge these
"Saves" is bytes freed on disk: APFS clones and hard links count only
the blocks no other file shares.
"""
import argparse, base64, calendar, ctypes, ctypes.util, hashlib, json, os, re, stat, struct, subprocess, sys, time, zipfile, zlib
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # -I drops the script folder
import owner

ME = owner.load()  # replaced in main() when --config is given

def work_named(name):
    return bool(ME.hint_re and ME.hint_re.search(name))

def named_me(name):
    return bool(ME.name_re and ME.name_re.search(name))

NOW = time.time()
DAY = 86400
MB = 1 << 20

# Folders a build tool recreates. Value: files that must sit next to the folder
# (any one), proving it belongs to a project. None = no manifest needed.
ARTIFACTS = {
    "node_modules": ("package.json",),
    ".next": ("package.json",), ".nuxt": ("package.json",), ".svelte-kit": ("package.json",),
    ".turbo": ("package.json",), ".parcel-cache": ("package.json",), ".expo": ("package.json",),
    "dist": ("package.json",), "coverage": ("package.json",),
    "build": ("pubspec.yaml", "package.json", "build.gradle", "build.gradle.kts"),
    ".dart_tool": ("pubspec.yaml",),
    "Pods": ("Podfile",),
    "DerivedData": None,
    ".gradle": ("build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts"),
    "target": ("Cargo.toml", "pom.xml"),
    "__pycache__": None, ".pytest_cache": None, ".mypy_cache": None, ".ruff_cache": None,
    ".venv": ("pyproject.toml", "requirements.txt", "setup.py"),
    "venv": ("pyproject.toml", "requirements.txt", "setup.py"),
}
# Never descend into these: app-managed data or app bundles. Deleting single
# files inside them corrupts the owner (a simulator, a library, a bundle).
SKIP_DIRS = {".git", ".Trash", ".Spotlight-V100", ".fseventsd", "Library", "CoreSimulator", "XCTestDevices"}
# Folders whose children are tool caches (TMPDIR, pub-cache, playwright browsers).
# Each child is reported whole for review, never deduped file by file.
CACHE_ROOTS = {"dev-cache"}
SKIP_SUFFIX = (".photoslibrary", ".app", ".xcarchive", ".musiclibrary", ".fcpbundle", ".imovielibrary")

SCREENSHOT = re.compile(r"^(Screenshot|Screen Shot|Screen Recording|CleanShot|Simulator Screen(shot| Recording)) .*\d{4}-\d{2}-\d{2}")
VERSIONED = re.compile(r"\d+\.\d+|arm64|x86_64|x64|universal", re.I)
PARTIAL = (".crdownload", ".part", ".partial", ".download")
ARCHIVE = (".zip",)
MEDIA = {".mov", ".mp4", ".m4v", ".mkv", ".avi", ".webm", ".wav", ".aif", ".aiff", ".flac", ".m4a", ".mp3",
         ".gif", ".webp", ".heif", ".avif", ".psd", ".tif", ".tiff", ".raw", ".cr2", ".arw", ".nef", ".dng", ".heic", ".png", ".jpg", ".jpeg"}

DUP_MIN = 128 * 1024     # exported images are often under 1 MB
STALE_MIN = 50 * MB       # "review" only for big files
STALE_DAYS = 365
MEDIA_STALE_DAYS = 180
SCREENSHOT_DAYS = 90
INSTALLER_DAYS = 30
ARTIFACT_ACTIVE_DAYS = 14  # project touched more recently than this: leave alone
ARTIFACT_SAFE_DAYS = 60
DOWNLOAD_REVIEW_DAYS = 60   # Downloads item untouched this long: worth a look (always "review")

# ---- disk accounting -------------------------------------------------------

_libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)

class _AttrList(ctypes.Structure):
    _fields_ = [("bitmapcount", ctypes.c_ushort), ("reserved", ctypes.c_ushort),
                ("commonattr", ctypes.c_uint), ("volattr", ctypes.c_uint), ("dirattr", ctypes.c_uint),
                ("fileattr", ctypes.c_uint), ("forkattr", ctypes.c_uint)]

_AL = _AttrList(5, 0, 0, 0, 0, 0, 0x8)  # ATTR_CMNEXT_PRIVATESIZE (forkattr slot when extended)
_BUF = ctypes.create_string_buffer(16)

def freed_bytes(path, st):
    """Bytes freed if this one file is deleted (unshared APFS blocks)."""
    if _libc.getattrlist(os.fsencode(path), ctypes.byref(_AL), _BUF, 16, 0x20 | 0x1) == 0:  # CMN_EXTENDED|NOFOLLOW
        return struct.unpack_from("<Iq", _BUF.raw)[1]
    return st.st_blocks * 512

def dir_freed(path):
    """Bytes freed by deleting a whole folder. A hard-linked file counts only if
    every link lives inside the folder (pnpm stores link out, so they don't)."""
    total, links = 0, {}
    for dp, dns, fns in os.walk(path):
        for fn in fns:
            p = os.path.join(dp, fn)
            try:
                st = os.lstat(p)
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            if st.st_nlink > 1:
                k = (st.st_dev, st.st_ino)
                seen, size = links.get(k, (0, 0))
                seen, size = seen + 1, size or freed_bytes(p, st)
                links[k] = (seen, size)
                if seen == st.st_nlink:  # last link found inside the folder
                    total += size
            else:
                total += freed_bytes(p, st)
    return total

# ---- signals ----------------------------------------------------------------

def _utc(v):
    v = v.decode(errors="ignore").strip()
    if not v or v == "(null)":
        return 0
    try:
        return calendar.timegm(time.strptime(v[:19], "%Y-%m-%d %H:%M:%S"))
    except ValueError:
        return 0

SPOT_FIELDS = sorted(["kMDItemAcquisitionModel", "kMDItemDateAdded", "kMDItemLastUsedDate", "kMDItemNumberOfPages",
                      "kMDItemImageIsScreenshot", "kMDItemContentCreationDate"])  # mdls -raw prints fields in this (sorted) order

def spotlight_meta(paths):
    """{path: (date_added, last_opened)} from Spotlight, 0 when unknown. Also
    fills CAMERA (camera model), PAGES (page count) and SHOT (macOS marked it a
    screenshot). Last opened is set when you open a file in an app; Spotlight,
    backups and this script reading the file don't change it."""
    out = {}
    args = [x for f in SPOT_FIELDS for x in ("-name", f)]
    k = len(SPOT_FIELDS)
    for i in range(0, len(paths), 200):
        chunk = paths[i:i + 200]
        try:
            raw = subprocess.run(["mdls", "-raw", *args, *chunk], capture_output=True, timeout=120).stdout.split(b"\0")
        except (OSError, subprocess.TimeoutExpired):
            continue
        if len(raw) < k * len(chunk):
            continue
        for j, p in enumerate(chunk):
            v = {f: raw[k * j + n] for n, f in enumerate(SPOT_FIELDS)}
            txt = lambda f: v[f].decode(errors="ignore").strip()
            if txt("kMDItemAcquisitionModel") not in ("", "(null)"):
                CAMERA[p] = txt("kMDItemAcquisitionModel")
            if txt("kMDItemNumberOfPages").isdigit():
                PAGES[p] = int(txt("kMDItemNumberOfPages"))
            if txt("kMDItemImageIsScreenshot") == "1":
                SHOT.add(p)
            if p in CAMERA:
                CAPTURED[p] = _utc(v["kMDItemContentCreationDate"])
            out[p] = (_utc(v["kMDItemDateAdded"]), _utc(v["kMDItemLastUsedDate"]))
    return out

CAMERA = {}
PAGES = {}
SHOT = set()
CAPTURED = {}  # camera photo -> capture time

def last_used(p, st, spot):
    """When you last added, changed, or opened the file; newest wins so we err
    toward keeping things. With Spotlight data the access time is ignored: any
    read (indexing, QuickLook thumbnails, backups) bumps it."""
    if p in spot and spot[p][0]:
        return max(st.st_mtime, *spot[p])
    return max(st.st_mtime, st.st_atime)

_git_cache = {}

def git_root(d):
    """Nearest enclosing git work tree, or None. Cached per directory."""
    chain = []
    while d not in _git_cache:
        chain.append(d)
        if os.path.exists(os.path.join(d, ".git")):
            _git_cache[d] = d
            break
        parent = os.path.dirname(d)
        if parent == d:
            _git_cache[d] = None
            break
        d = parent
    r = _git_cache[d]
    for c in chain:
        _git_cache[c] = r
    return r

def project_idle_days(project_dir, artifact_name):
    """Days since the project was last worked on: newest commit, or newest
    mtime among its other top-level entries, whichever is more recent."""
    newest = 0
    root = git_root(project_dir)
    if root:
        try:
            ct = subprocess.run(["git", "-C", root, "log", "-1", "--format=%ct"],
                                capture_output=True, text=True, timeout=20).stdout.strip()
            newest = int(ct or 0)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
    try:
        for e in os.scandir(project_dir):
            if e.name != artifact_name and e.name not in ARTIFACTS and e.name != ".git":
                newest = max(newest, e.stat(follow_symlinks=False).st_mtime)
    except OSError:
        pass
    return (NOW - newest) / DAY

def installed_apps():
    names = set()
    for d in ("/Applications", os.path.expanduser("~/Applications")):
        try:
            names |= {n[:-4].lower() for n in os.listdir(d) if n.endswith(".app")}
        except OSError:
            pass
    return names

def app_stem(filename):
    """'Figma-116.0-arm64.dmg' -> 'figma'."""
    stem = os.path.splitext(filename)[0]
    return re.split(r"[-_ ]v?\d", stem, maxsplit=1)[0].strip().lower()

def base_name(p):
    """'report (1).pdf' and 'report copy 2.pdf' -> 'report.pdf'."""
    stem, ext = os.path.splitext(os.path.basename(p))
    return re.sub(r"( \(\d+\)| copy( \d+)?)$", "", stem) + ext.lower()

def holds_protected(folder):
    """True if a git repo or app/library bundle lives anywhere inside."""
    for dp, dns, fns in os.walk(folder):
        if ".git" in dns or ".git" in fns or any(n.endswith(SKIP_SUFFIX) or n in SKIP_DIRS for n in dns):
            return True
    return False

def _top_flagged(p, dl_roots, by_path):
    """Is p, or the top-level Downloads item holding it, a flagged item?"""
    for d in dl_roots:
        if p.startswith(d + os.sep):
            return os.path.join(d, p[len(d) + 1:].split(os.sep, 1)[0]) in by_path
    return p in by_path

def zip_matches_folder(zpath, folder, top=None):
    """True when every file in the zip exists in folder with the same size.
    Accepts a zip whose entries sit under one top-level dir named like folder."""
    try:
        with zipfile.ZipFile(zpath) as z:
            infos = [i for i in z.infolist() if not i.is_dir() and not i.filename.startswith("__MACOSX/")]
    except (zipfile.BadZipFile, OSError):
        return False
    if not infos:
        return False
    base = top or os.path.basename(folder)
    hits = 0
    for i in infos:
        rel = i.filename
        if rel.startswith(base + "/"):
            rel = rel[len(base) + 1:]
        p = os.path.join(folder, rel)
        try:
            if os.path.getsize(p) == i.file_size and _crc32(p) == i.CRC:
                hits += 1
        except OSError:
            pass
    return hits == len(infos)

def _crc32(path):
    c = 0
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(4 * MB), b""):
            c = zlib.crc32(chunk, c)
    return c & 0xFFFFFFFF

def file_hash(path, partial=False):
    h = hashlib.blake2b(digest_size=20)
    with open(path, "rb") as f:
        if partial:
            h.update(f.read(64 * 1024))
            try:
                f.seek(-64 * 1024, 2)
            except OSError:
                pass
            h.update(f.read(64 * 1024))
        else:
            for chunk in iter(lambda: f.read(4 * MB), b""):
                h.update(chunk)
    return h.hexdigest()

# ---- copies elsewhere --------------------------------------------------------

# Where a second copy of a download might live. iCloud Drive counts: the file
# stays in the cloud and on every synced device.
# The folders themselves come from owner.load(): your home folders plus every mounted drive.
SF_DATALESS = 0x40000000   # iCloud placeholder: reading it would download it
# A copy inside these can vanish when a tool clears its output.
FRAGILE = re.compile(r"/(out|output|renders?|tmp|temp|cache|\.cache|fixtures?|\.ab-fixtures|build|dist)/", re.I)
JUNK_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini", ".localized"}

def where_label(p):
    home = os.path.expanduser("~")
    if "/Mobile Documents/com~apple~CloudDocs" in p:
        return "iCloud Drive"
    if p.startswith("/Volumes/"):
        parts = p.split("/")
        return f"the {parts[2]} drive ({parts[3]})" if len(parts) > 4 else f"the {parts[2]} drive"
    return "~/" + p[len(home) + 1:].split("/")[0] if p.startswith(home) else os.path.dirname(p)

def copies_elsewhere(cands, exclude):
    """cands: {path: size}, or {(zip, entry): (size, crc32)} for files inside
    a zip. Returns {key: keeper} for every candidate with an identical file
    under ME.index_roots, outside the `exclude` folders. Plain files: size, then
    head+tail hash, then full hash. Zip entries: size, then CRC32 (the
    checksum the zip itself stores)."""
    sizes = defaultdict(list)
    for p, s in cands.items():
        n = s[0] if isinstance(s, tuple) else s
        if n > 0:
            sizes[n].append(p)
    others = defaultdict(list)
    skip = SKIP_DIRS | set(ARTIFACTS) | CACHE_ROOTS
    seen = set()

    def walk(d):
        real = os.path.realpath(d)
        if real in seen or any(real == x or real.startswith(x + os.sep) for x in exclude):
            return
        seen.add(real)
        try:
            entries = list(os.scandir(d))
        except OSError:
            return
        for e in entries:
            if e.is_symlink():
                continue
            if e.is_dir(follow_symlinks=False):
                if e.name not in skip and not e.name.endswith(SKIP_SUFFIX):
                    walk(e.path)
            elif e.is_file(follow_symlinks=False):
                try:
                    st = e.stat(follow_symlinks=False)
                except OSError:
                    continue
                if st.st_size in sizes and not st.st_flags & SF_DATALESS:
                    others[st.st_size].append(e.path)

    for r in ME.index_roots:
        walk(os.path.expanduser(r))
    hashes = {}

    def h(p, partial):
        k = (p, partial)
        if k not in hashes:
            try:
                hashes[k] = file_hash(p, partial)
            except OSError:
                hashes[k] = None
        return hashes[k]

    crcs = {}

    def crc(q):
        if q not in crcs:
            try:
                crcs[q] = _crc32(q)
            except OSError:
                crcs[q] = None
        return crcs[q]

    found = {}
    for size, mine in sizes.items():
        theirs = others.get(size)
        if not theirs:
            continue
        # Prefer a visible copy (a repo, Documents) over hidden fixture folders.
        theirs.sort(key=lambda q: (bool(FRAGILE.search(q)), "/." in q, len(q)))
        for p in mine:
            for q in theirs:
                if isinstance(p, tuple):
                    if crc(q) == cands[p][1]:
                        found[p] = q
                        break
                elif h(p, True) and h(p, True) == h(q, True) and h(p, False) == h(q, False):
                    found[p] = q
                    break
    print(f"copy index: {sum(map(len, others.values()))} same-size files outside, "
          f"{len(found)} downloads have an exact copy", file=sys.stderr)
    return found

# ---- what an old download is -------------------------------------------------

DOC_EXT = {".pdf", ".doc", ".docx", ".pages", ".rtf", ".txt", ".odt"}
OFFICE_EXT = {".pptx", ".ppt", ".key", ".docx", ".doc", ".xlsx", ".xls", ".numbers", ".pages"}
RESUME = re.compile(r"resume|résumé|(?<![a-z])cv(?![a-z])", re.I)
AI_GEN = re.compile(r"^(Gemini_Generated_Image|ChatGPT Image|DALL[·.\-]?E|freepik__|magnific_|ElevenLabs_"
                    r"|output_progressive_|NotebookLM|Midjourney)|\d{8}_\d{4}_.+_(remix|simple_compose|compose)_[0-9a-z]{20,}"
                    r"|-translated\.(mp3|wav|m4a)$", re.I)
STOCK_SITE = re.compile(r"wallpapersden|pixabay|unsplash|pexels|freepik|shutterstock|istock|vecteezy|envato|-\d{6,}\.\w+$|^\d{6,}-", re.I)
DESIGN_EXT = {".fig", ".h2d", ".sketch", ".xd", ".ttf", ".otf", ".woff", ".woff2", ".ai", ".eps", ".psd",
              ".afdesign", ".aep", ".prproj", ".blend"}
EXPORT_NAME = re.compile(r"^(frame|group|rectangle|image|export|component|artboard|screens?|untitled|imgs+|out|v)"
                         r"[ _-]?[\d.\-]*( \(\d+\))?( [\d.]+ [AP]M)?\.(png|jpe?g|webp|svg)$"
                         r"|^\d+(\.\d+)+\.(png|jpe?g)$|_shots_so\.png$", re.I)
SCREEN_NAME = re.compile(r"^(screenshot|screen shot|screen recording|simulator screen|cleanshot)|^IMG_\d{4}\.PNG$"
                         r"|^IMG_[0-9A-F]{12}-\d", re.I)
SCREEN_DIR = re.compile(r"^(ss|screenshots?( \d+)?|screens)$", re.I)
CAMERA_NAME = re.compile(r"^(yes-|ss)?(DSC|DSCF|_DSC|DSC_|P\d{7}|IMG_\d{4}\.(jpe?g|heic))", re.I)
BOOK_NAME = re.compile(r"z-library|e-?book|guide|handbook|teardown|playbook|\bbook\b", re.I)
EXPORT_DATA = re.compile(r"dataexport|_rows\.csv$|takeout|^export_", re.I)
UUID_DIR = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
WORK_NAME = re.compile(r"store-ss|(?<![a-z])ads?(?![a-z])|ad-slide|pitch|proposal|deck|onboarding|progress report"
                       r"|content\.zip|(?<![a-z])content$|press release|prd|offsite|assignment|logo|insights", re.I)
LOCALE_DIR = re.compile(r"^(en|de|es|fr|hi|it|pt|ja|ko|zh|ar|ru|nl|tr|id)$")
RANDOM_NAME = re.compile(r"^([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[0-9a-f]{24,}|\d{15,})( \d+)?\.\w+$", re.I)
AD_SIZE = re.compile(r"(?<![0-9])(9x16|16x9|4x5|5x4|1x1|4x3|3x4)(?![0-9])")
VIDEO_AUDIO = {".mov", ".mp4", ".m4v", ".mkv", ".avi", ".webm", ".wav", ".aif", ".aiff", ".flac", ".m4a", ".mp3"}
IMAGE = MEDIA - VIDEO_AUDIO

# kind -> (tier, one-line reason shown per item)
KINDS = {
    "Copy kept elsewhere": ("safe", None),
    "Mostly copied elsewhere": ("likely", None),
    "AI-generated images & audio": ("likely", "made with an AI tool (ChatGPT, Gemini, Sora, Freepik, ElevenLabs)"),
    "Stock photos, music & templates": ("likely", "from a stock or wallpaper site; downloadable again"),
    "Design files, UI kits & fonts": ("likely", "design source or resource (.fig, .h2d, fonts); Figma and the original site keep a copy"),
    "Design exports & mockups": ("likely", "exported frame or mockup (Frame 123.png, image 2.jpg); re-export from the design file"),
    "Screenshots": ("likely", "screenshot or screen capture"),
    "App builds, data exports & code": ("likely", "old app build, account data export, or code download; rebuild or download again"),
    "Books, guides & long PDFs": ("likely", "ebook, guide or long published PDF; downloadable again"),
    "Hiring leftovers (résumés, job posts)": ("likely", "hiring paperwork: a candidate's résumé or take-home, or a job post that already ran"),
    "Trip quotes & itineraries": ("likely", "a travel agent's quote or itinerary; the agent can resend it"),
    "Pitches, offers & brochures": ("likely", "another company's sales pitch, price list, catalogue or course brochure"),
    "Past events & invites": ("likely", "an invite, poster or ticket for a dated event"),
    "Saved web pages": ("likely", "a web page printed to PDF from a browser; the page is still online"),
    "Exports from Google Docs & Canva": ("review", "exported from Google Docs or Canva with no other author named; if you made it, the original may still be there, but that can't be checked from here"),
    "Work files & decks": ("review", "your work (decks, proposals, documents); may be the only copy"),
    "Your ad & app-screen exports": ("likely", "exported images of your own ads, posts or app screens; re-export from your design file"),
    "Camera & phone photos": ("review", "photos from a camera or phone; may be personal"),
    "Photos of people": ("review", "a photo with people in it (Vision found a face); may be you or someone you know"),
    "Video & audio": ("review", "video or audio; may be a recording you made"),
    "Images with random web names": ("likely", "file name is a random ID (a1b2c3...), typical of images saved from a website or chat"),
    "Other images": ("review", "image with no clear source"),
    "Other documents": ("review", "document with no clear source"),
    "Everything else": ("review", "no rule matched"),
    "ID, tax & bank papers": ("keep", "looks like an ID, tax, bank or legal paper; kept out of the delete list"),
}

def personal_hits(dl_roots):
    """Files whose text mentions an ID, tax or bank term from your country pack (Spotlight full-text)."""
    hits = set()
    for d in dl_roots:
        for t in ME.id_terms:
            try:
                out = subprocess.run(["mdfind", "-onlyin", d, f'kMDItemTextContent == "*{t}*"cd'],
                                     capture_output=True, text=True, timeout=60).stdout
            except (OSError, subprocess.TimeoutExpired):
                continue
            hits.update(l for l in out.splitlines() if l)
    return hits

def text_personal(p, personal):
    """A text match counts unless the file is a long PDF: books mention
    "date of birth" too. Bank statements are caught by name instead."""
    return p in personal and PAGES.get(p, 0) < 40

def _zip_has_app(p):
    try:
        with zipfile.ZipFile(p) as z:
            return any(".app/" in n for n in z.namelist()[:50])
    except (zipfile.BadZipFile, OSError):
        return False

# ---- what a document was for -------------------------------------------------
# File type says what a file is; these say whose it is and whether its job is
# over. Text comes from PDFKit (via JavaScript for Automation, built into
# macOS), the rest from Spotlight metadata.

PDF_JS = """ObjC.import('PDFKit');
function run(argv) {
  const out = {};
  for (const p of argv) {
    const d = $.PDFDocument.alloc.initWithURL($.NSURL.fileURLWithPath(p));
    if (d.isNil()) { out[p] = ''; continue; }
    const s = d.string, a = d.documentAttributes;
    const au = (a.isNil() || a.objectForKey('Author').isNil()) ? '' : ObjC.unwrap(a.objectForKey('Author'));
    out[p] = [s.isNil() ? '' : ObjC.unwrap(s).slice(0, 30000), String(au)];
  }
  return JSON.stringify(out);
}"""
# One Vision pass per image: text (OCR), scene labels and face count. Run only
# on images a weak signal (name, OCR) is about to move.
LOOK_JS = """ObjC.import('Vision');
function run(argv) {
  const o = {};
  for (const p of argv) {
    const h = $.VNImageRequestHandler.alloc.initWithURLOptions($.NSURL.fileURLWithPath(p), $());
    const t = $.VNRecognizeTextRequest.alloc.init, c = $.VNClassifyImageRequest.alloc.init,
          f = $.VNDetectFaceRectanglesRequest.alloc.init;
    t.recognitionLevel = 1;
    h.performRequestsError($([t, c, f]), null);
    const words = [], labels = {};
    if (!t.results.isNil()) for (let i = 0; i < t.results.count; i++)
      words.push(ObjC.unwrap(t.results.objectAtIndex(i).topCandidates(1).objectAtIndex(0).string));
    if (!c.results.isNil()) for (let i = 0; i < c.results.count; i++) {
      const r = c.results.objectAtIndex(i);
      if (r.confidence > 0.3) labels[ObjC.unwrap(r.identifier)] = Number(r.confidence);
    }
    o[p] = {text: words.join(' '), labels: labels, faces: f.results.isNil() ? 0 : Number(f.results.count)};
  }
  return JSON.stringify(o);
}"""
DOCAUTHOR = {}  # path -> author named inside the PDF (Spotlight often says just "Canva")
LOOK = {}

def _look_batch(batch):
    try:
        out = subprocess.run(["osascript", "-l", "JavaScript", "-", *batch], input=LOOK_JS,
                             capture_output=True, text=True, timeout=300).stdout
        return json.loads(out)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return {}

def look_all(paths):
    todo = list(dict.fromkeys(p for p in paths if p not in LOOK))
    batches = [todo[i:i + 6] for i in range(0, len(todo), 6)]
    with ThreadPoolExecutor(max(1, min(6, (os.cpu_count() or 2) - 2))) as pool:  # Vision is CPU-bound per process
        for res in pool.map(_look_batch, batches):
            LOOK.update(res)
    for p in todo:
        LOOK.setdefault(p, {"text": "", "labels": {}, "faces": 0})

def look(p):
    look_all([p])
    return LOOK[p]

def ocr_text(p):
    return look(p)["text"]

# Burst shots: consecutive camera photos this close in Vision feature-print
# distance and capture time. An extra is listed only when it is this close to
# the shot that stays: in a judged sample all 12 extras within 0.145 were safe
# to drop, while from 0.15 to 0.18 a smile or a glance at the camera often changed.
BURST_DIST = 0.18
BURST_EXTRA = 0.145
BURST_GAP = 5
# Someone marked these shots as picks ("yes-DSC_123.jpg", "DSC_123 final.jpg"): they always stay.
PICKED = re.compile(r"^(yes|pick|picked|fav|best|select(ed)?|final)[-_ ]|[-_ ](final|pick|fav|selected|edit(ed)?)\b", re.I)
FPRINT_JS = """ObjC.import('Vision');
function run(argv) {
  const o = {};
  for (const p of argv) {
    const h = $.VNImageRequestHandler.alloc.initWithURLOptions($.NSURL.fileURLWithPath(p), $());
    const f = $.VNGenerateImageFeaturePrintRequest.alloc.init;
    const reqs = [f];
    const A = $.VNCalculateImageAestheticsScoresRequest;  // macOS 15+; older systems keep the earliest shot
    const a = A ? A.alloc.init : null;
    if (a) reqs.push(a);
    h.performRequestsError($(reqs), null);
    if (f.results.isNil() || !f.results.count) continue;
    const r = f.results.objectAtIndex(0);
    const s = a && !a.results.isNil() && a.results.count ? Number(a.results.objectAtIndex(0).overallScore) : 0;
    o[p] = [ObjC.unwrap(r.data.base64EncodedStringWithOptions(0)), Number(r.elementType), Number(r.elementCount), s];
  }
  return JSON.stringify(o);
}"""

def _fprint_batch(batch):
    try:
        out = subprocess.run(["osascript", "-l", "JavaScript", "-", *batch], input=FPRINT_JS,
                             capture_output=True, text=True, timeout=600).stdout
        return json.loads(out)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return {}

def bursts(photos):
    """[(best, [extras])] for runs of near-identical camera shots. `photos` are
    paths with a capture time in CAPTURED. The shot that stays is the one
    Vision's aesthetics score rates highest; scores within a burst differ by a few
    hundredths, so the review tier leaves the final pick to you."""
    photos = sorted(photos, key=lambda p: (CAPTURED[p], p))
    prints = {}
    with ThreadPoolExecutor(max(1, min(6, (os.cpu_count() or 2) - 2))) as pool:
        for res in pool.map(_fprint_batch, [photos[i:i + 20] for i in range(0, len(photos), 20)]):
            for p, (b64, kind, n, score) in res.items():
                fmt = "f" if kind == 1 else "d"
                raw = base64.b64decode(b64)
                prints[p] = (struct.unpack(f"<{n}{fmt}", raw[:n * struct.calcsize(fmt)]), score)
    dist = lambda a, b: sum((x - y) ** 2 for x, y in zip(prints[a][0], prints[b][0])) ** 0.5
    groups, cur = [], []
    for p in (p for p in photos if p in prints):
        if cur and CAPTURED[p] - CAPTURED[cur[-1]] <= BURST_GAP and dist(cur[0], p) <= BURST_DIST:
            cur.append(p)
        else:
            cur = [p]
            groups.append(cur)
    out = []
    for g in groups:
        if len(g) > 1:
            picked = [p for p in g if PICKED.search(os.path.basename(p))]
            best = max(picked or g, key=lambda p: prints[p][1])
            extras = [p for p in g if p != best and p not in picked and dist(p, best) <= BURST_EXTRA]
            if extras:
                out.append((best, extras))
    return out

def shows_people(p):
    """A photo of real people: Vision is sure it sees people and finds a face.
    Illustrated ads score well under 0.8 on "people"."""
    v = look(p)
    return int(v["faces"]) >= 1 and float(v["labels"].get("people", 0)) >= 0.8

DOCTEXT = {}   # path -> first 6000 characters of PDF text
DOCMETA = {}   # path -> authors, creator app and title, joined
OWN_SPOT = set()  # Spotlight text hits for your name or company
TRAVEL_META = re.compile(r"sembark|wanderon|makemytrip|thrillophilia|skyhike|12go|yatra|goibibo|cleartrip"
                         r"|expedia|booking\.com|airbnb|tripadvisor|kayak|trip\.com|agoda", re.I)
INVITE = re.compile(r"invite|invitation|wedding|poster|ticket|(?<![a-z])boarding|rsvp|गृह प्रवेश|निमंत्रण", re.I)
PITCH_NAME = re.compile(r"packages?(?![a-z])|(?<![a-z])offer(?! letter)|collab card|rate card|media kit|brochure|catalog"
                        r"|certification|course|-from-", re.I)
MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10,
          "nov": 11, "dec": 12, "जनवरी": 1, "फ़रवरी": 2, "फरवरी": 2, "मार्च": 3, "अप्रैल": 4, "मई": 5, "जून": 6,
          "जुलाई": 7, "अगस्त": 8, "सितंबर": 9, "सितम्बर": 9, "अक्टूबर": 10, "नवंबर": 11, "नवम्बर": 11,
          "दिसंबर": 12, "दिसम्बर": 12}
_MON = "|".join(sorted(MONTHS, key=len, reverse=True))
DATE_DMY = re.compile(r"(?<!\d)(\d{1,2})(?:st|nd|rd|th)?[\s\-_/.,]*(" + _MON + r")[a-z]*[^\d]{0,16}?(20\d{2})", re.I)
DATE_MDY = re.compile(r"(" + _MON + r")[a-z]*[\s.\-]+(\d{1,2})(?:st|nd|rd|th)?,?[\s\-]+(20\d{2})", re.I)
DATE_ISO = re.compile(r"(20\d{2})-(\d{2})-(\d{2})")

def load_doc_signals(paths):
    pdfs = [p for p in paths if p.lower().endswith(".pdf")]
    for i in range(0, len(pdfs), 40):
        try:
            out = subprocess.run(["osascript", "-l", "JavaScript", "-", *pdfs[i:i + 40]], input=PDF_JS,
                                 capture_output=True, text=True, timeout=180).stdout
            for k, (txt, au) in json.loads(out).items():
                if txt:
                    DOCTEXT[k] = txt
                if au.strip():
                    DOCAUTHOR[k] = au.strip()
        except (OSError, subprocess.TimeoutExpired, ValueError):
            pass
    docs = [p for p in paths if os.path.splitext(p)[1].lower() in DOC_EXT | OFFICE_EXT]
    for i in range(0, len(docs), 200):
        chunk = docs[i:i + 200]
        try:
            raw = subprocess.run(["mdls", "-raw", "-name", "kMDItemAuthors", "-name", "kMDItemCreator",
                                  "-name", "kMDItemEncodingApplications", "-name", "kMDItemTitle", *chunk],
                                 capture_output=True, timeout=120).stdout.split(b"\0")
        except (OSError, subprocess.TimeoutExpired):
            continue
        if len(raw) < 4 * len(chunk):
            continue
        for j, p in enumerate(chunk):
            DOCMETA[p] = " ".join(re.sub(r'[()"\n]+', " ", x.decode(errors="ignore")).strip()
                                  for x in raw[4 * j:4 * j + 4] if x.strip() != b"(null)")
    for t in ME.spotlight_terms:
        for root in {p.split("/Downloads/")[0] + "/Downloads" for p in paths if "/Downloads/" in p}:
            try:
                out = subprocess.run(["mdfind", "-onlyin", root, f'kMDItemTextContent == "{t}"cdw'],  # whole word only: a name inside a longer word is not you
                                     capture_output=True, text=True, timeout=60).stdout
                OWN_SPOT.update(l for l in out.splitlines() if l)
            except (OSError, subprocess.TimeoutExpired):
                pass

def squashed(p):
    return re.sub(r"\s+", "", DOCTEXT.get(p, "")).lower()

def owned(p, name):
    """Your name or company appears in the file name, its metadata, or its text."""
    return bool(ME.in_name(name) or p in OWN_SPOT or ME.in_text(DOCMETA.get(p, ""))
                or ME.in_text(DOCAUTHOR.get(p, "")) or ME.in_text(DOCTEXT.get(p, "")))

def doc_dates(p):
    """Dates named in the file name, title, or first page or so of text."""
    hay = " ".join((os.path.basename(p), DOCMETA.get(p, ""), DOCTEXT.get(p, "")))
    found = []
    for m in DATE_DMY.finditer(hay):
        found.append((int(m.group(3)), MONTHS[next(k for k in MONTHS if m.group(2).lower().startswith(k))], int(m.group(1))))
    for m in DATE_MDY.finditer(hay):
        found.append((int(m.group(3)), MONTHS[next(k for k in MONTHS if m.group(1).lower().startswith(k))], int(m.group(2))))
    for m in DATE_ISO.finditer(hay):
        found.append((int(m.group(1)), int(m.group(2)), int(m.group(3))))
    ok = []
    for y, mo, d in found:
        try:
            ok.append(calendar.timegm((y, mo, d, 0, 0, 0)))
        except (ValueError, OverflowError):
            pass
    return [t for t in ok if 2000 <= time.gmtime(t).tm_year <= 2100 and 1 <= time.gmtime(t).tm_mday <= 31]

def purpose_kind(p, name, ext):
    """Whose file is this and is its job done? None when no signal fires or
    the file mentions you (your own work never moves on these signals)."""
    if ext not in DOC_EXT | OFFICE_EXT | IMAGE or owned(p, name):
        return None
    kind = _purpose(p, name, ext)
    if kind and ext in IMAGE and ME.in_text(ocr_text(p)):
        return None  # your name or brand is in the picture
    return kind

def received_from(p):
    """The person named as author inside the PDF, when it isn't you."""
    a = DOCAUTHOR.get(p, "")
    return a if a and not ME.in_text(a) and not re.search(r"canva|microsoft|adobe|google|^u-\d+$", a, re.I) else ""

def _purpose(p, name, ext):
    text, meta = squashed(p), DOCMETA.get(p, "")
    if ext in DOC_EXT:
        cv = sum(w in text for w in ("experience", "education", "skills", "projects", "linkedin",
                                     "internship", "bachelor", "university", "b.tech"))
        if cv >= 3 and PAGES.get(p, 1) <= 4 and not PITCH_NAME.search(name):
            return "Hiring leftovers (résumés, job posts)"
    if re.search(r"^hiring|we.?re hiring|job description|job post", name, re.I):
        return "Hiring leftovers (résumés, job posts)"
    if TRAVEL_META.search(meta) or re.search(r"itinerary|tour package|holiday package", name, re.I) or (
            ("itinerary" in text or "inclusions" in text) and ("day1" in text or "nights" in text)
            and PAGES.get(p, 1) <= 30):
        return "Trip quotes & itineraries"
    if ext == ".pdf" and re.fullmatch(r"1[5-9]\d{11}\.pdf", name):
        return "Saved web pages"  # LinkedIn names document downloads with a millisecond timestamp
    if received_from(p) and re.search(r"teardown|case ?study|assignment|take.?home", name + " " + text, re.I):
        return "Hiring leftovers (résumés, job posts)"
    if PITCH_NAME.search(name) or "proprietaryto" in text:
        return "Pitches, offers & brochures"
    if INVITE.search(name):
        return "Past events & invites"
    if meta.startswith("Mozilla/"):
        return "Saved web pages"
    if ("Google Docs Renderer" in meta or re.search(r"(?<![a-z])canva(?![a-z])", meta, re.I)) and not received_from(p):
        return "Exports from Google Docs & Canva"
    return None

PURPOSE_KINDS = {"Hiring leftovers (résumés, job posts)", "Trip quotes & itineraries", "Pitches, offers & brochures",
                 "Past events & invites", "Saved web pages", "Exports from Google Docs & Canva"}
DATED_KINDS = {"Trip quotes & itineraries", "Past events & invites"}

def dated_tier(p, kind):
    """Trips and events: likely once the latest date in the file has passed,
    review while it's still ahead."""
    ds = doc_dates(p)
    if not ds:
        if kind == "Past events & invites":  # an event with an unread date may be ahead
            return "review", "; no date could be read, so it may still be ahead"
        return KINDS[kind][0], "; no date found, check it's over"
    last = max(ds)
    when = time.strftime("%d %b %Y", time.gmtime(last))
    if last >= NOW - DAY:
        return "review", f"; latest date in it is {when}, still ahead"
    return KINDS[kind][0], f"; latest date in it is {when}, already passed"

def file_kind(p, personal):
    name = os.path.basename(p)
    ext = os.path.splitext(name)[1].lower()
    if RESUME.search(name) and not named_me(name) and ext in DOC_EXT:
        return "Hiring leftovers (résumés, job posts)"
    if text_personal(p, personal) or ME.id_name_re.search(name) or (named_me(name) and ext in DOC_EXT):
        return "ID, tax & bank papers"
    why = purpose_kind(p, name, ext)
    if why:
        return why
    if ext in DESIGN_EXT or re.search(r"ui kit|design system|mockup|template", name, re.I):
        return "Design files, UI kits & fonts"
    if ext in IMAGE and ai_provenance(p):
        return "AI-generated images & audio"
    if RANDOM_NAME.match(name) and ext in IMAGE:
        return "Images with random web names"
    if AI_GEN.search(name):
        return "AI-generated images & audio"
    if STOCK_SITE.search(name) or (re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+){4,}\.\w+", name)
                                   and not re.search(r"\d+x\d+", name) and not ME.in_name(name)):
        return "Stock photos, music & templates"
    if (ext == ".zip" and VERSIONED.search(name) and _zip_has_app(p)) or EXPORT_DATA.search(name):
        return "App builds, data exports & code"
    if SCREEN_NAME.search(name):
        return "Screenshots"
    if p in CAMERA or CAMERA_NAME.search(name):
        return "Camera & phone photos"
    if EXPORT_NAME.search(name):
        return "Design exports & mockups"
    if ext == ".pdf" and BOOK_NAME.search(name):
        return "Books, guides & long PDFs"
    if WORK_NAME.search(name) or work_named(name) or ext in OFFICE_EXT or (ext in IMAGE and AD_SIZE.search(name)):
        return "Your ad & app-screen exports" if ext in IMAGE else "Work files & decks"
    if ext == ".pdf" and PAGES.get(p, 0) >= 40:
        return "Books, guides & long PDFs"
    if ext in VIDEO_AUDIO:
        return "Video & audio"
    if ext in IMAGE:
        return "Other images"
    if ext in DOC_EXT:
        return "Other documents"
    return "Everything else"

PROV = {}

def ai_provenance(p):
    """("made" | "edited", tool) when the image carries C2PA / IPTC AI provenance
    (Sora, ChatGPT, Gemini, Firefly write it), else None. The marker sits in the
    first or last 128 KB of the file."""
    if p not in PROV:
        PROV[p] = None
        try:
            with open(p, "rb") as f:
                blob = f.read(1 << 17)
                size = f.seek(0, 2)
                if size > 1 << 17:
                    f.seek(max(1 << 17, size - (1 << 17)))
                    blob += f.read()
        except OSError:
            return None
        how = ("edited" if b"compositeWithTrainedAlgorithmicMedia" in blob
               else "made" if b"trainedAlgorithmicMedia" in blob else None)
        if how:
            PROV[p] = (how, _c2pa_tool(blob))
    return PROV[p]

def _c2pa_tool(blob):
    """Name of the app that signed the C2PA claim, read from its CBOR text string."""
    i = blob.find(b"claim_generator_info")
    j = blob.find(b"dname", i) if i >= 0 else -1
    if j < 0 or j + 7 >= len(blob):
        return ""
    b = blob[j + 5]
    n, start = (b - 0x60, j + 6) if 0x60 <= b <= 0x77 else (blob[j + 6], j + 7) if b == 0x78 else (0, j)
    return blob[start:start + n].decode("utf-8", "ignore").split(" C2PA")[0].strip()

# AI tools that edit a photo you upload (retouch, upscale, headshot), unlike ones that make an image from a prompt.
AI_EDIT = re.compile(r"^freepik__(adjust|enhance|upscale|relight|retouch|reimagine)|^magnific_|headshot|upscal|retouch", re.I)
PEOPLE_CHECK = {"Images with random web names", "Your ad & app-screen exports"}

def weak_image(p, kind):
    """An image about to move on its name alone."""
    if os.path.splitext(p)[1].lower() not in IMAGE:
        return False
    edited = AI_EDIT.search(os.path.basename(p)) or (ai_provenance(p) or ("",))[0] == "edited"
    return kind in PEOPLE_CHECK or (kind == "AI-generated images & audio" and bool(edited))

def look_targets(path, is_dir, members, kind):
    """Images worth a Vision pass: a weak move to check, or a leftover image whose
    text may name you (up to 3 of a folder's biggest images)."""
    if not is_dir:
        return [path] if weak_image(path, kind) or kind == "Other images" else []
    if kind != "Other images":
        return []
    imgs = [m for m in members if os.path.splitext(m)[1].lower() in IMAGE]
    return sorted(imgs, key=lambda m: -os.path.getsize(m))[:3]

def second_look(path, is_dir, members, kind):
    """Look inside images before a weak signal moves them, and place leftover
    images by what macOS or their own text says."""
    if kind == "Other images":
        sample = look_targets(path, is_dir, members, kind)
        if sample and sum(m in SHOT for m in sample) * 2 > len(sample):
            return "Screenshots"
        if sample and sum(ME.in_text(ocr_text(m)) for m in sample) * 2 > len(sample):
            kind = "Your ad & app-screen exports"
    if not is_dir and weak_image(path, kind) and shows_people(path):
        return "Photos of people"
    return kind

def unit_kind(path, is_dir, members, sizes, personal):
    """Kind of a top-level Downloads item. A folder takes the kind holding most
    of its bytes, except: one personal paper inside makes the whole folder
    personal, and a telling folder name wins."""
    if not is_dir:
        return file_kind(path, personal)
    name = os.path.basename(path)
    kinds = {m: file_kind(m, personal) for m in members}
    named = [m for m, k in kinds.items() if k == "ID, tax & bank papers"]
    if ME.id_name_re.search(name) or any(text_personal(m, personal) for m in members) or len(named) * 3 >= len(members):
        return "ID, tax & bank papers"
    if sum(sizes[m] for m in members if m in CAMERA) * 2 >= sum(sizes.values()):
        return "Camera & phone photos"
    if UUID_DIR.match(name) or re.search(r"-(main|master)$|dataexport", name, re.I):
        return "App builds, data exports & code"
    if any(n.endswith((".eps", ".ai")) for n in map(str.lower, members)) and any(
            n.lower().endswith(".txt") for n in members):
        return "Stock photos, music & templates"  # stock packs ship a license .txt
    if WORK_NAME.search(name) or work_named(name) or LOCALE_DIR.match(name):
        img = sum(sizes[m] for m in members if os.path.splitext(m)[1].lower() in IMAGE)
        return "Your ad & app-screen exports" if img >= 0.9 * sum(sizes.values()) else "Work files & decks"
    if SCREEN_DIR.match(name):
        return "Screenshots"
    by = defaultdict(int)
    for m, k in kinds.items():
        by[k] += sizes[m]
    return max(by, key=by.get)

# ---- scan ------------------------------------------------------------------

def disk_label(path):
    return "external" if path.startswith("/Volumes/") else "internal"

def scan(roots):
    items, files, artifacts, caches = [], [], [], []
    apps = installed_apps()

    def add(path, category, tier, saves, reason, used=None, extra=None):
        it = {"path": path, "category": category, "tier": tier, "saves": saves,
              "disk": disk_label(path), "reason": reason}
        if used:
            it["last_used"] = time.strftime("%Y-%m-%d", time.localtime(used))
        if extra:
            it.update(extra)
        items.append(it)

    def walk(d):
        try:
            entries = list(os.scandir(d))
        except OSError:
            return
        names = {e.name for e in entries}
        for e in entries:
            if e.is_symlink():
                continue
            p = e.path
            if e.is_dir(follow_symlinks=False):
                if e.name in SKIP_DIRS or e.name.endswith(SKIP_SUFFIX):
                    continue
                if e.name in CACHE_ROOTS:
                    try:
                        caches.extend(c.path for c in os.scandir(p) if c.is_dir(follow_symlinks=False))
                    except OSError:
                        pass
                    continue
                need = ARTIFACTS.get(e.name, False)
                if need is not False and (need is None or any(n in names for n in need)):
                    if e.name == ".venv" or e.name == "venv":
                        if not os.path.exists(os.path.join(p, "pyvenv.cfg")):
                            walk(p)
                            continue
                    artifacts.append((p, d, e.name))
                    continue  # rebuildable as a unit; don't look inside
                walk(p)
            elif e.is_file(follow_symlinks=False):
                try:
                    st = e.stat(follow_symlinks=False)
                except OSError:
                    continue
                files.append((p, st))

    for r in roots:
        walk(os.path.realpath(os.path.expanduser(r)))
    print(f"walked: {len(files)} files, {len(artifacts)} build folders", file=sys.stderr)

    # Build folders: whole-folder items, tiered by how long the project sat idle.
    seen_artifacts = set()
    for p, proj, name in artifacts:
        # Nested artifacts (node_modules inside dist) are covered by the outer one.
        if any(p.startswith(a + os.sep) for a in seen_artifacts):
            continue
        seen_artifacts.add(p)
        idle = project_idle_days(proj, name)
        if idle < ARTIFACT_ACTIVE_DAYS:
            continue
        saves = dir_freed(p)
        if saves < MB:
            continue
        tier = "safe" if idle >= ARTIFACT_SAFE_DAYS else "likely"
        add(p, "Build folders", tier, saves,
            f"{name} rebuilds from {proj.rsplit('/', 1)[-1]}; project idle {int(idle)} days")

    for p in caches:
        saves = dir_freed(p)
        if saves >= 10 * MB:
            add(p, "Tool caches", "review", saves,
                "tool cache or temp folder; the tool re-downloads or recreates it, but check nothing is running")

    # Inside a git work tree only build folders are fair game: untracked files
    # there (.env, local configs) are often the only copy.
    loose = [(p, st) for p, st in files if not git_root(os.path.dirname(p))]
    dl_roots = [os.path.realpath(os.path.expanduser(r)) for r in roots
                if os.path.basename(os.path.realpath(os.path.expanduser(r))) == "Downloads"]
    in_dl = lambda p: any(p.startswith(d + os.sep) for d in dl_roots)
    internal = [p for p, st in loose if disk_label(p) == "internal" and (st.st_size >= DUP_MIN or in_dl(p))]
    spot = spotlight_meta(internal)
    info = {p: (st, last_used(p, st, spot)) for p, st in loose}

    # Downloads is a landing zone: each top-level item (file or folder) is one
    # unit, dated by the newest add/change/open anywhere inside. Found first so
    # duplicate handling can avoid keeping a copy that sits inside one.
    units = []
    for d in dl_roots:
        try:
            entries = list(os.scandir(d))
        except OSError:
            continue
        for e in entries:
            if e.is_symlink() or e.name.startswith("."):
                continue
            is_dir = e.is_dir(follow_symlinks=False)
            if is_dir:
                if holds_protected(e.path):
                    continue  # a repo or app bundle inside: deleting the folder takes it too
                members = [p for p in info if p.startswith(e.path + os.sep)]
            else:
                members = [e.path] if e.path in info else []
            if not members:
                continue
            newest = max(info[m][1] for m in members)
            if (NOW - newest) / DAY >= DOWNLOAD_REVIEW_DAYS:
                units.append((e.path, is_dir, members, newest))
    unit_paths = {u[0] for u in units}

    def in_unit(p):
        """Would deleting some flagged Downloads unit take p with it?"""
        for d in dl_roots:
            if p.startswith(d + os.sep):
                return os.path.join(d, p[len(d) + 1:].split(os.sep, 1)[0]) in unit_paths
        return False

    flagged = set()
    depends = {}  # item path -> path that must survive for the item to be safe
    by_size = defaultdict(list)
    for p, st in loose:
        name = os.path.basename(p)
        low = name.lower()
        ext = os.path.splitext(low)[1]
        used = info[p][1]
        age = (NOW - used) / DAY

        if low.endswith(PARTIAL):
            add(p, "Unfinished downloads", "safe", freed_bytes(p, st), "partial download that never completed", used)
            flagged.add(p); continue
        if ext in (".dmg", ".pkg"):
            stem = app_stem(name)
            if stem in apps:
                add(p, "Installers", "safe", freed_bytes(p, st), f"an app named {stem} is in Applications", used)
                flagged.add(p); continue
            if VERSIONED.search(name) and age >= INSTALLER_DAYS:
                add(p, "Installers", "likely", freed_bytes(p, st), f"versioned installer, untouched {int(age)}+ days", used)
                flagged.add(p); continue
            # A plain disk image can hold anything (tax files, backups): no rule.
        if ext in (".exe", ".msi"):
            add(p, "Installers", "likely", freed_bytes(p, st), "Windows installer; it can't run on this Mac", used)
            flagged.add(p); continue
        if ext in ARCHIVE:
            stem = name[:-len(ext)]
            parent = os.path.dirname(p)
            try:
                sibs = [os.path.join(parent, n) for n in sorted(os.listdir(parent))
                        if (n == stem or n.startswith(stem + "-") or n.startswith(stem + " "))
                        and os.path.isdir(os.path.join(parent, n))]
            except OSError:
                sibs = []
            match = next((f for f in sibs if zip_matches_folder(p, f, stem)), None)
            if match:
                add(p, "Already-extracted zips", "safe", freed_bytes(p, st),
                    f"every file is already in {os.path.basename(match)}/ (same size and checksum)", used)
                depends[p] = match
                flagged.add(p); continue
        if SCREENSHOT.match(name) and age >= SCREENSHOT_DAYS:
            add(p, "Old screenshots", "likely", freed_bytes(p, st), f"screenshot, untouched {int(age)}+ days", used)
            flagged.add(p); continue
        if ext == ".log" and st.st_size >= 10 * MB:
            add(p, "Big logs", "likely", freed_bytes(p, st), "log file over 10 MB", used)
            flagged.add(p); continue
        if st.st_size >= DUP_MIN and st.st_nlink == 1:
            by_size[st.st_size].append((p, st, used))

    # Exact duplicates: same size -> same head+tail hash -> same full hash.
    for size, group in by_size.items():
        if len(group) < 2:
            continue
        heads = defaultdict(list)
        for g in group:
            try:
                heads[file_hash(g[0], partial=True)].append(g)
            except OSError:
                pass
        for hg in heads.values():
            if len(hg) < 2:
                continue
            fulls = defaultdict(list)
            for g in hg:
                try:
                    fulls[file_hash(g[0])].append(g)
                except OSError:
                    pass
            for same in fulls.values():
                same = [g for g in same if g[0] not in flagged]
                if len(same) < 2:
                    continue
                # Keep a copy no other flag would delete, then one outside
                # Downloads, then the shallowest path, then the oldest.
                same.sort(key=lambda g: (in_unit(g[0]), "/Downloads/" in g[0], g[0].count("/"), g[1].st_mtime))
                keep = same[0][0]
                if keep in unit_paths:
                    unit_paths.discard(keep)  # a loose file: protect it so the copies are safe to drop
                keep_at_risk = in_unit(keep)
                for p, st, used in same[1:]:
                    saves = freed_bytes(p, st)
                    if saves < 4096:
                        continue  # APFS clone of the kept file: deleting frees nothing
                    tier, reason = "safe", "exact copy of the kept file"
                    if keep_at_risk:
                        tier, reason = "review", "exact copy, but every copy is inside an old download flagged below; keep one"
                    elif os.path.dirname(p) == os.path.dirname(keep) and base_name(p) == base_name(keep):
                        reason = "re-downloaded or Finder copy of the kept file"
                    elif os.path.dirname(p) == os.path.dirname(keep):
                        tier, reason = "review", "exact copy with a different name in the same folder; may be part of a numbered set"
                    elif not (in_dl(p) and os.path.dirname(p) in dl_roots):
                        tier, reason = "likely", "exact copy kept in another folder; this folder loses its copy"
                    add(p, "Duplicates", tier, saves, reason, used, {"keeps": keep})
                    depends[p] = keep
                    flagged.add(p)

    # Big files nobody has touched in a long time (outside Downloads, where the
    # unit rule below covers them).
    for p, st in loose:
        if p in flagged or st.st_size < STALE_MIN or in_dl(p):
            continue
        age = (NOW - info[p][1]) / DAY
        ext = os.path.splitext(p)[1].lower()
        if age >= (MEDIA_STALE_DAYS if ext in MEDIA else STALE_DAYS):
            add(p, "Big and untouched", "review", freed_bytes(p, st),
                f"{'media' if ext in MEDIA else 'file'}, not opened or changed in {int(age)}+ days", info[p][1])
            flagged.add(p)

    # Old downloads, split by what they are. Age only makes an item a candidate
    # (untouched 60+ days); the kind sets the confidence. Migrating to a new Mac
    # wipes open history, so a missing open date is unknown, not "never opened".
    live = [u for u in units if u[0] not in flagged and u[0] in unit_paths]
    # Burst shots inside folders of camera photos: list the extras as their own
    # items (review), so the folder's own item counts only what's left.
    for u in live:
        shots = [m for m in u[2] if m in CAMERA and CAPTURED.get(m) and os.path.splitext(m)[1].lower() in IMAGE]
        if not u[1] or len(shots) < 4:
            continue
        for best, extras in bursts(shots):
            for x in extras:
                gap = abs(CAPTURED[x] - CAPTURED[best])
                add(x, "Near-duplicate photos", "review", freed_bytes(x, info[x][0]),
                    f"burst shot with the same pose and framing as {os.path.basename(best)} ({int(gap)}s apart), "
                    f"which stays; compare the two before deleting", info[x][1], {"keeps": best})
    personal = personal_hits(dl_roots)
    load_doc_signals([m for u in live for m in u[2]])
    cand = {m: info[m][0].st_size for u in live for m in u[2]}
    zips = {}
    for u in live:
        if not u[1] and u[0].lower().endswith(".zip"):
            try:
                with zipfile.ZipFile(u[0]) as z:
                    ents = [i for i in z.infolist() if not i.is_dir() and i.file_size > 0
                            and not i.filename.startswith("__MACOSX/")
                            and os.path.basename(i.filename) not in JUNK_NAMES]
            except (zipfile.BadZipFile, OSError):
                continue
            zips[u[0]] = [(u[0], i.filename) for i in ents]
            cand.update({(u[0], i.filename): (i.file_size, i.CRC) for i in ents})
    elsewhere = copies_elsewhere(cand, dl_roots)
    look_all([m for u in live for m in look_targets(u[0], u[1], u[2], unit_kind(u[0], u[1], u[2],
              {x: info[x][0].st_size for x in u[2]}, personal))])
    keepers = {}  # item path -> every outside copy it relies on
    for path, is_dir, members, newest in live:
        inside_flagged = sum(i["saves"] for i in items if i["path"].startswith(path + os.sep))
        total = dir_freed(path) if is_dir else freed_bytes(path, info[path][0])
        saves = total - inside_flagged
        if saves < MB:
            continue
        age = (NOW - newest) / DAY
        opened = max((spot.get(m, (0, 0))[1] for m in members), default=0)
        when = (f"last opened {time.strftime('%Y-%m-%d', time.localtime(opened))}" if opened
                else "no open recorded")
        sizes = {m: info[m][0].st_size for m in members}
        real = [m for m in members if sizes[m] > 0 and os.path.basename(m) not in JUNK_NAMES]
        copied = [m for m in real if m in elsewhere]
        count = f"{len(members)} files, " if len(members) > 1 else ""
        extra = {"old": True}
        if zips.get(path) and all(e in elsewhere for e in zips[path]):
            kind = "Copy kept elsewhere"
            ks = [elsewhere[e] for e in zips[path]]
            places = sorted({where_label(k) for k in ks})
            top = os.path.commonpath(ks) if len({where_label(k) for k in ks}) == 1 else None
            reason = (f"all {len(ks)} files inside are already unzipped in "
                      + (top.replace(os.path.expanduser("~"), "~") if top and top.count("/") > 2 else ", ".join(places)))
            keepers[path] = ks
            extra["keeps"] = ks[0]
            extra["keeps_all"] = [[e[1], k] for e, k in zip(zips[path], ks)]
        elif real and len(copied) == len(real):
            kind = "Copy kept elsewhere"
            places = sorted({where_label(elsewhere[m]) for m in copied})
            reason = f"{count}exact copy also in {', '.join(places)}"
            keepers[path] = [elsewhere[m] for m in copied]
            extra["keeps"] = elsewhere[copied[0]]
            extra["keeps_all"] = [[m, elsewhere[m]] for m in copied]
        else:
            parts = [(e, cand[e][0]) for e in zips[path]] if zips.get(path) else [(m, sizes[m]) for m in real]
            whole = sum(n for _, n in parts) or 1
            lost = [k for k, n in parts if k not in elsewhere]
            copied_b = sum(n for k, n in parts if k in elsewhere)
            kind = second_look(path, is_dir, members, unit_kind(path, is_dir, members, sizes, personal))
            if lost and copied_b >= 0.9 * whole and whole - copied_b < 50 * MB and kind != "ID, tax & bank papers":
                kind = "Mostly copied elsewhere"
                have = [elsewhere[k] for k, _ in parts if k in elsewhere]
                ex = os.path.basename(lost[0][1] if isinstance(lost[0], tuple) else lost[0])
                reason = (f"{fmt_mb(copied_b)} of {fmt_mb(whole)} is already in "
                          f"{', '.join(sorted({where_label(k) for k in have}))}; {len(lost)} file(s) "
                          f"({fmt_mb(whole - copied_b)}) exist only here, e.g. {ex}")
                keepers[path] = have
                extra["keeps"] = have[0]
                extra["keeps_all"] = [[k[1] if isinstance(k, tuple) else k, elsewhere[k]] for k, _ in parts if k in elsewhere]
                copied = []
            else:
                reason = KINDS[kind][1]
                if not is_dir and kind == "AI-generated images & audio" and PROV.get(path):
                    how, tool = PROV[path]
                    reason += f"; its content credentials say {how} with {tool or 'an AI tool'}"
                if not is_dir and ME.in_text(LOOK.get(path, {}).get("text", "")):
                    reason += "; the image shows your name or brand, so it's likely your own"
                if not is_dir and kind in PURPOSE_KINDS:
                    if re.fullmatch(r"1[5-9]\d{11}\.pdf", os.path.basename(path)):
                        reason = "a document saved from LinkedIn (the file name is a download timestamp)"
                    if received_from(path):
                        reason += f"; author inside the PDF: {received_from(path)}"
                copied = [k for k, _ in parts if k in elsewhere]
                sizes.update({k: n for k, n in parts})
        if kind not in ("Copy kept elsewhere", "Mostly copied elsewhere"):
            cams = sorted({CAMERA[m] for m in members if m in CAMERA})
            if kind == "Camera & phone photos" and cams:
                reason += f" ({', '.join(cams[:2])})"
            if is_dir and kind != "ID, tax & bank papers":
                mix = defaultdict(int)
                for m in members:
                    mix[file_kind(m, personal)] += 1
                if len(mix) > 1:
                    reason += "; inside: " + ", ".join(f"{n} {k.split(',')[0].split(' &')[0].lower()}"
                                                      for k, n in sorted(mix.items(), key=lambda kv: -kv[1])[:3])
            part = sum(sizes[m] for m in copied)
            if part:
                reason += f"; {fmt_mb(part)} of it also exists outside Downloads"
        tier = KINDS[kind][0]
        if kind in DATED_KINDS and not is_dir:
            tier, note = dated_tier(path, kind)
            reason += note
        frail = [k for k in keepers.get(path, []) if FRAGILE.search(k)]
        if frail:
            m = FRAGILE.search(frail[0]).group(1)
            reason += (f"; {len(frail)} of the outside copies {'is' if len(frail) == 1 else 'are'} in a folder "
                       f"named {m}/, which tools may clear")
            tier = "likely" if tier == "safe" else tier
        if is_dir and kind != "ID, tax & bank papers":
            odd = [os.path.basename(m) for m in members if m not in elsewhere and file_kind(m, personal) == "ID, tax & bank papers"]
            if odd:
                reason += f"; holds {len(odd)} file(s) that look personal ({odd[0][:40]})"
                tier = "review" if tier in ("safe", "likely") else tier
        add(path, kind, tier, saves, f"{count}{reason}; untouched {int(age)}+ days, {when}"
            if kind not in ("Copy kept elsewhere", "Mostly copied elsewhere") else f"{reason}; untouched {int(age)}+ days",
            newest, extra)
        flagged.add(path)

    # Last check: a "safe" item is only safe if what it relies on survives.
    by_path = {i["path"]: i for i in items}

    def at_risk(dep):
        if dep in by_path or _top_flagged(dep, dl_roots, by_path):
            return True
        d = os.path.dirname(dep)
        while d and d != "/":  # inside a flagged folder (build folder, cache, ...)
            if d in by_path:
                return True
            d = os.path.dirname(d)
        return False

    for i in items:
        deps = keepers.get(i["path"]) or ([depends[i["path"]]] if i["path"] in depends else [])
        hit = next((d for d in deps if at_risk(d)), None)
        if i["tier"] in ("safe", "likely") and hit and (i["tier"] == "safe" or i["path"] in keepers):
            i["tier"] = "review"
            i["reason"] += f"; but {os.path.basename(hit)} is also flagged, so delete only one"

    return items

def fmt_mb(b):
    return f"{b / (1 << 30):.1f} GB" if b >= 1 << 30 else f"{max(1, round(b / MB))} MB"

def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("roots", nargs="+")
    ap.add_argument("--out", default=".")
    ap.add_argument("--config", help="owner config JSON (see owner.py)")
    a = ap.parse_args()
    global ME
    ME = owner.load(a.config)
    t = time.time()
    items = scan(a.roots)
    items.sort(key=lambda i: -i["saves"])
    report = {"generated": time.strftime("%Y-%m-%d %H:%M"), "roots": a.roots,
              "seconds": round(time.time() - t), "items": items}
    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "report.json"), "w") as f:
        json.dump(report, f, indent=1)
    summary = defaultdict(lambda: [0, 0])
    for i in items:
        s = summary[(i["category"], i["tier"], i["disk"])]
        s[0] += 1; s[1] += i["saves"]
    for (c, t_, d), (n, b) in sorted(summary.items(), key=lambda kv: -kv[1][1]):
        print(f"{c:24} {t_:7} {d:8} {n:6} items {b / (1 << 30):7.2f} GB")
    print(f"done in {report['seconds']}s -> {os.path.join(a.out, 'report.json')}", file=sys.stderr)

if __name__ == "__main__":
    main()
