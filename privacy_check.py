"""Refuse to publish personal data. Run before every push: python3 -I privacy_check.py

Fails when a tracked file contains
  - a name, handle or brand from your owner config,
  - an email address or a phone-number-like digit run,
  - the name of any real file in the folders you scanned (default ~/Downloads),
  - any word from those file names that the system dictionary doesn't know
    (people's names, places, companies), unless listed in PUBLIC_WORDS,
  - any extra string listed in private/denylist.txt (one per line).
Images (README screenshots) are checked by the text Vision reads in them.
LICENSE may carry your name as the copyright holder.
"""
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import owner  # noqa: E402

IMAGES = (".png", ".jpg", ".jpeg", ".gif", ".webp")

EMAIL = re.compile(rb"[\w.+-]+@[\w-]+(\.[A-Za-z]{2,})+")
PHONE = re.compile(rb"(?<![\w.])(\+?\d[\d -]{9,}\d)(?![\w.])")
ALLOWED_EMAILS = {b"noreply@anthropic.com"}
SKIP = {"LICENSE"}
GENERIC = {"store-ss"}  # rule vocabulary that also happens to be a folder name


def tracked():
    out = subprocess.run(["git", "-C", HERE, "ls-files", "-z"], capture_output=True, check=True).stdout
    return [p for p in out.decode().split("\0") if p]


# Public words that appear in scanned file names and also in this code: tool and
# site names, ID-term vocabulary. Reviewed by hand; a person's name never goes here.
PUBLIC_WORDS = set("""aadhaar automation chatgpt collab config ebook elevenlabs email figma freepik github google
download gstin headshot hiring kotak linkedin localized notebooklm offsite onboarding online payslip quicklook screenshot
wallpapersden workflow""".split())


def _english(word, dic):
    w = word.lower()
    return any(x in dic for x in (w, w[:-1], w[:-2], w[:-3], w[:-1] + "e" if w.endswith("d") else w))


def name_words(scan_roots):
    """Words in scanned file names that no dictionary knows: mostly names of people, places and companies."""
    try:
        dic = {w.strip().lower() for w in open("/usr/share/dict/words")}
    except OSError:
        return set()
    out = set()
    for root in scan_roots:
        for d, dirs, files in os.walk(os.path.expanduser(root)):
            for n in dirs + files:
                for t in re.findall(r"[A-Za-z]{5,}", os.path.splitext(n)[0]):
                    if not _english(t, dic) and t.lower() not in PUBLIC_WORDS and t.lower()[:-1] not in PUBLIC_WORDS:
                        out.add(t.lower())
    return out


def denylist(me, scan_roots):
    words = {w for w in me.names + me.handles + me.brands if len(w) >= 3}
    for root in scan_roots:
        for d, dirs, files in os.walk(os.path.expanduser(root)):
            for n in dirs + files:
                stem = os.path.splitext(n)[0]
                # Single plain words ("download", "screenshots") are vocabulary, not names.
                if len(stem) >= 8 and not stem.startswith(".") and not re.fullmatch(r"[A-Za-z]+", stem) \
                        and stem.lower() not in GENERIC:
                    words.add(stem)
    extra = os.path.join(HERE, "private", "denylist.txt")
    if os.path.exists(extra):
        words |= {l.strip() for l in open(extra) if l.strip()}
    return sorted(words, key=len, reverse=True)


def main():
    me = owner.load()
    roots = sys.argv[1:] or ["~/Downloads"]
    words = denylist(me, roots)
    pat = re.compile(b"|".join(re.escape(w.lower().encode()) for w in words))
    names = name_words(roots)
    name_pat = re.compile(rb"(?<![a-z])(" + b"|".join(re.escape(w.encode()) for w in sorted(names)) + rb")(?![a-z])") \
        if names else None
    fails = []
    shots = [os.path.join(HERE, r) for r in tracked() if r.lower().endswith(IMAGES)]
    if shots:
        import declutter  # Vision OCR; only loaded when there are images to read
        declutter.look_all(shots)
    for rel in tracked():
        if rel in SKIP or rel.startswith("private/"):
            if rel.startswith("private/"):
                fails.append(f"{rel}: private/ must never be tracked")
            continue
        full = os.path.join(HERE, rel)
        data = declutter.LOOK[full]["text"].encode() if rel.lower().endswith(IMAGES) else open(full, "rb").read()
        low = data.lower()
        for m in pat.finditer(low):
            if low[max(0, m.start() - 11):m.start()] == b"github.com/":  # your own repo URL
                continue
            line = low[:m.start()].count(b"\n") + 1
            fails.append(f"{rel}:{line}: personal string {m.group().decode(errors='replace')!r}")
        for m in (name_pat.finditer(low) if name_pat else []):
            line = low[:m.start()].count(b"\n") + 1
            fails.append(f"{rel}:{line}: name-like word from your files {m.group().decode(errors='replace')!r}")
        for m in EMAIL.finditer(data):
            if m.group() not in ALLOWED_EMAILS:
                fails.append(f"{rel}: email {m.group().decode(errors='replace')}")
        for m in PHONE.finditer(data):
            if sum(c.isdigit() for c in m.group().decode()) < 10:
                continue
            fails.append(f"{rel}: phone-like number {m.group().decode(errors='replace')}")
    print(f"{len(tracked())} tracked files checked against {len(words)} personal strings and {len(names)} "
          f"name-like words, {len(fails)} problems")
    for f in fails[:50]:
        print("  FAIL", f)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
