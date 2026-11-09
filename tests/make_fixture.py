"""Build a fake Downloads folder with one file per rule, all made up.

Usage: python3 -I tests/make_fixture.py DIR
Creates DIR/Downloads, DIR/Documents (the "elsewhere" copy) and DIR/config.json
for a made-up owner, Jane Doe, whose app is called AcmeApp. Returns nothing;
EXPECTED maps each relative path to the (category, tier) the scanner should give it.
"""
import json
import os
import struct
import sys
import time

import zlib

DAY = 86400
OLD = time.time() - 200 * DAY

EXPECTED = {
    "Downloads/Gemini_Generated_Image_ab12cd.png": ("AI-generated images & audio", "likely"),
    "Downloads/sunset.png": ("AI-generated images & audio", "likely"),  # C2PA says AI-made
    "Downloads/mountain-landscape-with-lake-and-pine-forest.jpg": ("Stock photos, music & templates", "likely"),
    "Downloads/Mobile UI Kit.fig": ("Design files, UI kits & fonts", "likely"),
    "Downloads/Frame 1234.png": ("Design exports & mockups", "likely"),
    "Downloads/Robin Roe Resume.pdf": ("Hiring leftovers (résumés, job posts)", "likely"),
    "Downloads/Bank Statement March.pdf": ("ID, tax & bank papers", "keep"),
    "Downloads/W-2 2024.pdf": ("ID, tax & bank papers", "keep"),
    "Downloads/acme-launch-ad-4x5.png": ("Your ad & app-screen exports", "likely"),
    "Downloads/en": ("Your ad & app-screen exports", "likely"),
    "Downloads/AcmeApp Pitch.pptx": ("Work files & decks", "review"),
    "Downloads/notes.txt": ("Other documents", "review"),
    "Downloads/report-copy.pdf": ("Copy kept elsewhere", "safe"),
    "Downloads/video.mp4.crdownload": ("Unfinished downloads", "safe"),
}


def png(width=8, height=8, extra=b""):
    """A valid tiny PNG; `extra` goes in a private ancillary chunk (where C2PA lives in real files)."""
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    raw = b"".join(b"\0" + b"\x80" * (3 * width) for _ in range(height))
    out = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    if extra:
        out += chunk(b"caBX", extra)
    return out + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


def pdf(text):
    """A one-page PDF with `text` in it, padded past the scanner's 1 MB floor."""
    body = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
            b"/Resources << /Font << /F1 5 0 R >> >> >>",
            b"<< /Length %d >>\nstream\n" % len(body) + body + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offs = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offs.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1) + b"".join(b"%010d 00000 n \n" % o for o in offs)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return out + b"%" + b" " * (1 << 20) + b"\n"


PAD = b"\0" * (1 << 20)  # old downloads under 1 MB are ignored


def build(root):
    dl, docs = os.path.join(root, "Downloads"), os.path.join(root, "Documents")
    os.makedirs(os.path.join(dl, "en"), exist_ok=True)
    os.makedirs(docs, exist_ok=True)
    c2pa = (b"c2pa.created claim_generator_info\xa1dnamedSora "
            b"http://cv.iptc.org/newscodes/digitalsourcetype/trainedAlgorithmicMedia")
    files = {
        "Gemini_Generated_Image_ab12cd.png": png(extra=PAD),
        "sunset.png": png(extra=c2pa + PAD),
        "mountain-landscape-with-lake-and-pine-forest.jpg": PAD,
        "Mobile UI Kit.fig": PAD,
        "Frame 1234.png": png(extra=PAD),
        "Robin Roe Resume.pdf": pdf("Robin Roe resume"),
        "Bank Statement March.pdf": pdf("statement"),
        "W-2 2024.pdf": pdf("wages"),
        "acme-launch-ad-4x5.png": png(extra=PAD),
        "AcmeApp Pitch.pptx": PAD,
        "notes.txt": b"meeting notes\n" + PAD,
        "report-copy.pdf": pdf("quarterly numbers"),
        "video.mp4.crdownload": PAD,
    }
    for i in range(3):
        files[f"en/store-{i}.png"] = png(extra=PAD + bytes([i]))
    for name, data in files.items():
        with open(os.path.join(dl, name), "wb") as f:
            f.write(data + name.encode())  # unique bytes, so no two files are duplicates
    with open(os.path.join(docs, "report.pdf"), "wb") as f:
        f.write(files["report-copy.pdf"] + b"report-copy.pdf")
    for d, _, fs in os.walk(root):
        for n in fs:
            os.utime(os.path.join(d, n), (OLD, OLD))
    with open(os.path.join(root, "config.json"), "w") as f:
        json.dump({"names": ["Jane", "Doe"], "handles": ["janedoe"], "brands": ["AcmeApp"],
                   "work_hints": ["acme"], "country": "US", "index_roots": [docs]}, f)


if __name__ == "__main__":
    build(sys.argv[1])
