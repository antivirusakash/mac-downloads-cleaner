"""Score a declutter report against hand labels.

labels: JSONL rows {"path", "label": DELETE_OK|KEEP|UNSURE, ...} (or {"id"} plus an items.json that maps id -> path).
Items in the "Copy kept elsewhere" group count as correct whatever the label says, because check.py proves
a byte-identical copy survives.
"""
import argparse
import json
import os
import sys

MOVE = {"safe", "likely"}


def load_labels(label_dir):
    items = {}
    items_path = os.path.join(label_dir, "items.json")
    if os.path.exists(items_path):
        items = {r["id"]: r["path"] for r in json.load(open(items_path))}
    labels = {}
    for name in sorted(os.listdir(label_dir)):
        if not name.endswith(".jsonl"):
            continue
        for line in open(os.path.join(label_dir, name)):
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            path = row.get("path") or items.get(row.get("id"))
            if path:
                labels[path] = row
    return labels


def score(report, labels):
    old = [i for i in report["items"] if i.get("old")]
    rows = []
    for item in old:
        lab = labels.get(item["path"])
        if lab:
            rows.append((item, lab["label"]))
    moved = [(i, l) for i, l in rows if i["tier"] in MOVE]
    proven = [(i, l) for i, l in moved if i["category"] == "Copy kept elsewhere"]
    judged = [(i, l) for i, l in moved if i["category"] != "Copy kept elsewhere"]
    good = sum(1 for _, l in judged if l == "DELETE_OK") + len(proven)
    wrong_keep = [(i, l) for i, l in judged if l == "KEEP"]
    unsure = [(i, l) for i, l in judged if l == "UNSURE"]
    ok_all = [(i, l) for i, l in rows if l == "DELETE_OK"]
    ok_moved = [(i, l) for i, l in ok_all if i["tier"] in MOVE]
    keep_all = [(i, l) for i, l in rows if l == "KEEP"]
    keep_held = [(i, l) for i, l in keep_all if i["tier"] == "keep"]
    b = lambda xs: sum(i["saves"] for i, _ in xs)
    return {
        "labelled": len(rows),
        "unlabelled": len(old) - len(rows),
        "moved": len(moved),
        "precision": good / len(moved) if moved else 1.0,
        "keep_in_moved": len(wrong_keep),
        "keep_in_moved_mb": b(wrong_keep) / 1e6,
        "unsure_in_moved": len(unsure),
        "recall_items": len(ok_moved) / len(ok_all) if ok_all else 0.0,
        "recall_bytes": b(ok_moved) / b(ok_all) if ok_all else 0.0,
        "deletable_mb": b(ok_all) / 1e6,
        "moved_ok_mb": b(ok_moved) / 1e6,
        "keep_tier_recall": len(keep_held) / len(keep_all) if keep_all else 0.0,
        "_wrong": wrong_keep + unsure,
        "_missed": sorted((x for x in ok_all if x[0]["tier"] not in MOVE), key=lambda x: -x[0]["saves"]),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("report")
    ap.add_argument("labels", help="folder of *.jsonl label files")
    ap.add_argument("--log", help="append a row to this decision.tsv")
    ap.add_argument("--note", default="")
    ap.add_argument("--show", type=int, default=15)
    a = ap.parse_args()
    s = score(json.load(open(a.report)), load_labels(a.labels))
    print(f"labelled {s['labelled']} (unlabelled {s['unlabelled']}), moved to safe/likely {s['moved']}")
    print(f"precision {s['precision']:.1%}   KEEP in moved {s['keep_in_moved']} ({s['keep_in_moved_mb']:.0f} MB)   UNSURE in moved {s['unsure_in_moved']}")
    print(f"recall {s['recall_items']:.1%} of deletable items, {s['recall_bytes']:.1%} of deletable bytes "
          f"({s['moved_ok_mb']:.0f} of {s['deletable_mb']:.0f} MB)")
    print(f"KEEP-labelled items held in keep tier {s['keep_tier_recall']:.1%}")
    if s["_wrong"]:
        print("\nwrongly moved:")
        for i, l in s["_wrong"]:
            print(f"  {l:8} {i['tier']:6} {i['category'][:28]:28} {i["saves"]/1e6:7.1f} MB  {os.path.basename(i['path'])[:50]}")
    if a.show:
        print("\nbiggest missed (DELETE_OK but not moved):")
        for i, _ in s["_missed"][: a.show]:
            print(f"  {i['tier']:6} {i['category'][:28]:28} {i["saves"]/1e6:7.1f} MB  {os.path.basename(i['path'])[:50]}")
    if a.log:
        new = not os.path.exists(a.log)
        with open(a.log, "a") as f:
            if new:
                f.write("note\tprecision\tkeep_in_moved\tunsure_in_moved\trecall_items\trecall_bytes\tmoved_ok_mb\n")
            f.write(f"{a.note}\t{s['precision']:.3f}\t{s['keep_in_moved']}\t{s['unsure_in_moved']}\t"
                    f"{s['recall_items']:.3f}\t{s['recall_bytes']:.3f}\t{s['moved_ok_mb']:.0f}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
