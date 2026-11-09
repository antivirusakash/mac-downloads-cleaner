#!/usr/bin/env python3
"""Render report.json from declutter.py into a single preview page.
Usage: python3 -I render.py out/report.json preview.html [--total-bytes N] [--label ~/Downloads]"""
import argparse, html, json, os

ap = argparse.ArgumentParser()
ap.add_argument("report")
ap.add_argument("out")
ap.add_argument("--total-bytes", type=int, default=0, help="size of the scanned folder, for the bar")
ap.add_argument("--label", default="~/Downloads")
a = ap.parse_args()

r = json.load(open(a.report))
home = os.path.expanduser("~")
for i in r["items"]:
    i["path"] = i["path"].replace(home, "~")
    i["reason"] = i["reason"].replace(home + "/", "~/")
    if "keeps" in i:
        i["keeps"] = i["keeps"].replace(home, "~")
    if "keeps_all" in i:
        i["more_keeps"] = len({k for _, k in i.pop("keeps_all")}) - 1
data = json.dumps({"generated": r["generated"], "label": a.label, "total": a.total_bytes, "items": r["items"]})
data = data.replace("</", "<\\/")

page = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "template.html")).read()
page = page.replace("__DATA__", data).replace("__LABEL__", html.escape(a.label))
open(a.out, "w").write(page)
print(f"wrote {a.out} ({len(page) // 1024} KB, {len(r['items'])} items)")
