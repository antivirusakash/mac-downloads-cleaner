#!/usr/bin/env python3
"""Invariants every declutter report must satisfy. Each one is a bug found in
review. Usage: python3 -I check.py report.json [--config owner.json]"""
import filecmp, json, os, sys, zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import owner

ME = owner.load(sys.argv[sys.argv.index("--config") + 1] if "--config" in sys.argv else None)

items = json.load(open(sys.argv[1]))["items"]
paths = {i["path"]: i for i in items}
fails = []
COPY_KINDS = ("Copy kept elsewhere", "Mostly copied elsewhere")

def covered(p):
    """p is flagged itself or sits inside a flagged folder."""
    while p and p != "/":
        if p in paths:
            return paths[p]
        p = os.path.dirname(p)
    return None

def same_bytes(item, inner, keeper):
    """Does the file at `inner` (path, or entry name inside a zip) match keeper byte for byte?"""
    if item["path"].lower().endswith(".zip") and not os.path.isabs(inner):
        with zipfile.ZipFile(item["path"]) as z, z.open(inner) as a, open(keeper, "rb") as b:
            while True:
                x, y = a.read(1 << 20), b.read(1 << 20)
                if x != y:
                    return False
                if not x:
                    return True
    return filecmp.cmp(inner, keeper, shallow=False)

checked = 0
for i in items:
    # 1. A safe item never depends on something another flag would delete.
    dep = i.get("keeps") or (i["reason"].split("already in ")[1].split("/")[0] if "already in " in i["reason"] and i["category"] not in COPY_KINDS else None)
    if i["tier"] == "safe" and dep:
        dep_path = dep if dep.startswith("/") else os.path.join(os.path.dirname(i["path"]), dep)
        hit = covered(dep_path)
        if hit:
            fails.append(f"safe item relies on a flagged copy: {i['path']} -> {hit['path']} ({hit['category']})")
    # 2. Missing open history is unknown, never "never opened".
    if "never opened" in i["reason"]:
        fails.append(f"claims 'never opened': {i['path']}")
    # 3. Old downloads: only a verified copy is safe; personal papers stay out;
    #    photos never above review.
    if i.get("old"):
        if i["tier"] == "safe" and i["category"] != "Copy kept elsewhere":
            fails.append(f"old download marked safe without a copy: {i['path']}")
        if i["category"] == "ID, tax & bank papers" and i["tier"] != "keep":
            fails.append(f"personal paper not kept: {i['path']}")
        if i["category"] == "Camera & phone photos" and i["tier"] not in ("review", "keep"):
            fails.append(f"photos above review: {i['path']}")
    # 4. No item counted inside another item (double counting).
    parent = covered(os.path.dirname(i["path"]))
    if parent and not (i["category"] in ("Duplicates", "Already-extracted zips", "Near-duplicate photos") and parent.get("old")):
        fails.append(f"counted twice: {i['path']} inside {parent['path']}")
    # 5. Only real installers are called installers.
    if i["category"] == "Installers" and i["tier"] == "safe" and not i["path"].lower().endswith((".dmg", ".pkg")):
        fails.append(f"non-mac file marked installed: {i['path']}")
    if i["saves"] < 0:
        fails.append(f"negative saves: {i['path']}")
    # 6. Every claimed outside copy exists outside the scanned folder and is byte-identical.
    if i["category"] in COPY_KINDS:
        pairs = i.get("keeps_all") or []
        if not pairs:
            fails.append(f"copy claim with no copies listed: {i['path']}")
        for inner, keeper in pairs:
            if "/Downloads/" in keeper or not os.path.exists(keeper):
                fails.append(f"bad keeper {keeper} for {i['path']}")
            elif not same_bytes(i, inner, keeper):
                fails.append(f"not identical: {inner} vs {keeper}")
            checked += 1
        if i["category"] == "Copy kept elsewhere" and os.path.isdir(i["path"]):
            listed = {a for a, _ in pairs}
            for dp, _, fns in os.walk(i["path"]):
                for f in fns:
                    fp = os.path.join(dp, f)
                    if fp not in listed and os.path.getsize(fp) > 0 and f not in (".DS_Store", "Thumbs.db", "desktop.ini", ".localized"):
                        fails.append(f"folder marked copied has an uncopied file: {fp}")

# 7. Purpose signals: no moved file mentions you, and no "likely" trip or
#    event points at a future year. Text is read here independently of the
#    scanner (PDFKit through JavaScript for Automation).
PURPOSE = {"Hiring leftovers (résumés, job posts)", "Trip quotes & itineraries", "Pitches, offers & brochures",
           "Past events & invites", "Saved web pages", "Exports from Google Docs & Canva"}
JS = """ObjC.import('PDFKit');
function run(argv) { const o = {}; for (const p of argv) { const d = $.PDFDocument.alloc.initWithURL($.NSURL.fileURLWithPath(p));
  o[p] = (d.isNil() || d.string.isNil()) ? '' : ObjC.unwrap(d.string); } return JSON.stringify(o); }"""
moved = [i for i in items if i["category"] in PURPOSE]
pdfs = [i["path"] for i in moved if i["path"].lower().endswith(".pdf")]
import re, subprocess
texts = json.loads(subprocess.run(["osascript", "-l", "JavaScript", "-", *pdfs], input=JS, capture_output=True,
                                  text=True).stdout or "{}") if pdfs else {}
for i in moved:
    t = texts.get(i["path"], "")
    flat = re.sub(r"\s+", "", t).lower()
    name = os.path.basename(i["path"]).lower()
    if i["path"].lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".gif", ".heic")):
        ocr = subprocess.run(["osascript", "-l", "JavaScript", "-", i["path"]], capture_output=True, text=True, input="""ObjC.import('Vision');
function run(a){const q=$.VNRecognizeTextRequest.alloc.init;const h=$.VNImageRequestHandler.alloc.initWithURLOptions($.NSURL.fileURLWithPath(a[0]),$());
h.performRequestsError($([q]),null);const r=q.results,o=[];if(!r.isNil())for(let i=0;i<r.count;i++)o.push(ObjC.unwrap(r.objectAtIndex(i).topCandidates(1).objectAtIndex(0).string));return o.join(' ');}""").stdout
        if ME.in_text(ocr):
            fails.append(f"moved image shows your name or brand: {i['path']}")
    if ME.in_text(t) or ME.in_name(name):
        fails.append(f"moved file mentions you: {i['path']}")
    if i["category"] in ("Trip quotes & itineraries", "Past events & invites") and i["tier"] == "likely":
        if re.search(r"\b20(2[7-9]|[3-9]\d)\b", t + name) or "still ahead" in i["reason"]:
            fails.append(f"likely trip/event may be in the future: {i['path']}")
    if i["category"] == "Past events & invites" and i["tier"] == "likely" and "already passed" not in i["reason"]:
        fails.append(f"event in likely without a past date read from it: {i['path']}")

print(f"{len(moved)} purpose moves re-read for your name and future dates")
print(f"{len(items)} items, {checked} outside copies compared byte for byte, {len(fails)} failures")
for f in fails[:30]:
    print("  FAIL", f)
sys.exit(1 if fails else 0)
