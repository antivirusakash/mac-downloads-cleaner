# declutter

A preview-only cleanup scanner for a Mac's Downloads folder. It reads your files, works out what each one is and whether you still need it, and writes a report plus a one-page HTML preview grouped by kind. It never deletes, moves or uploads anything.

Most cleanup tools sort by size and age, which can't tell a résumé from a finished hiring round apart from a travel quote for a trip still ahead or the only copy of your passport scan. This tool reads each file to tell them apart, using the metadata macOS already keeps and a few on-device models.

## What it looks at

| Signal | Source | Used for |
|---|---|---|
| Byte-identical copy elsewhere | size, then head+tail hash, then full hash; zip entries by CRC32 | "Copy kept elsewhere" (the only old-download group marked safe) |
| Date added, last opened, camera model, page count, screenshot flag, capture time | Spotlight (`mdls`) | age, camera photos, long PDFs, screenshots |
| ID, tax and bank terms inside text | Spotlight full-text search, with a word list per country | the keep group |
| PDF text and the author named inside the PDF | PDFKit | résumés, travel quotes, sales pitches, past events, pages printed from a browser |
| Text in images, scene labels, faces | Vision | your own brand in an ad, photos of real people held back |
| AI provenance (C2PA / IPTC `trainedAlgorithmicMedia`) | the file's own bytes | AI-generated images, and which tool made them |
| Feature prints, aesthetics scores, capture times | Vision, Spotlight | near-identical burst shots; a shot whose name marks it a pick stays, else the best-rated one |
| Dates inside documents | PDF text (English and Hindi month names) | trips and events count as over only after their last date |

Everything runs on your Mac with the system Python and `osascript`. Nothing is installed and nothing goes over the network.

## Tiers

- **safe**: a byte-identical copy exists somewhere that stays, or the file is junk by construction (an unfinished download, a zip already extracted next to itself).
- **likely**: things you can get again or whose job is over: AI images, stock files, UI kits, design exports, other people's résumés, past trips and events.
- **review**: may be the only copy of your work or memories: decks, camera photos, recordings, anything no rule recognised.
- **keep**: ID, tax, bank and legal papers. Shown so you can see the space, never ticked by default.

Your own files never move on a purpose signal. A file counts as yours when your name, handle or brand appears in its name, metadata, PDF text, or (for images) the text Vision reads in it.

## Run it

```sh
git clone https://github.com/antivirusakash/declutter && cd declutter
./run.sh                    # scans ~/Downloads, writes out/report.json and preview.html
DIR=~/Desktop ./run.sh      # any other folder
open preview.html
```

Or step by step:

```sh
python3 -I declutter.py --out out ~/Downloads
python3 -I check.py out/report.json          # invariants every report must satisfy
python3 -I render.py out/report.json preview.html --total-bytes $(du -sk ~/Downloads | awk '{print $1*1024}')
```

A scan of a 7 GB Downloads folder with 4,500 files takes about 75 seconds on an M-series Mac mini, most of it Vision.

## Tell it who you are

Out of the box it uses your full name from `id -F`, your login name, your region from System Settings (which picks the ID-term list), and every mounted drive as a place a second copy might live. Add what it can't know in `~/.config/declutter/config.json` (or pass `--config`):

```json
{
  "names": ["Jane", "Doe"],
  "handles": ["janedoe"],
  "brands": ["AcmeApp"],
  "work_hints": ["acme"],
  "country": "US"
}
```

`brands` and `work_hints` mark files as your own work. `country` picks extra ID terms (`IN`, `US` and `GB` ship today; adding one is a few lines in `owner.py`). `index_roots`, if set, replaces the default list of places searched for copies.

## How it was measured

On the author's own Downloads, every one of the 290 items untouched for 60+ days was labelled DELETE_OK, KEEP or UNSURE by reviewer agents that opened each file and never saw the scanner's verdict. `eval.py` scores a report against such labels:

| Version | Precision of safe + likely | Keep-worthy items wrongly in safe/likely | Deletable bytes found |
|---|---|---|---|
| Name and type rules only | 97.1% | 2 | 78.9% |
| + exports of your own work split from decks | 97.5% | 2 | 92.8% |
| + Vision people check | 99.0% | 0 | 92.7% |
| + C2PA provenance | 99.0% | 0 | 93.9% |
| + OCR brand and screenshot flag on leftover images | 99.0% | 0 | 94.3% |

Burst detection is scored separately: a reviewer compared each proposed extra with the shot that stays, and 11 of 12 were safe to drop (one had a different expression), so that group stays in review.

The rules were tuned on the same items they are scored on, so treat these as an upper bound for your folder. The synthetic fixture in `tests/` exercises every rule without any real files.

To score it on your own files, put JSONL rows like `{"path": "/Users/you/Downloads/x.pdf", "label": "KEEP"}` in `private/labels/` and run `./run.sh`.

## Tests

```sh
python3 -I -m unittest discover -s tests
```

`tests/make_fixture.py` builds a fake Downloads folder for a made-up owner, one file per rule, and the end-to-end test checks each lands in its group and that `check.py` passes.

## Privacy

Reports and previews contain your file names and stay in `out/`, which git ignores. Before pushing changes, `python3 -I privacy_check.py` fails if any tracked file contains your configured names or brands, an email address, a phone number, or the name of any file in the folders you scanned.

## Limits

- macOS only (Spotlight, PDFKit, Vision). Aesthetics scores need macOS 15; on older systems a burst keeps its earliest shot.
- Dates in scripts other than Latin and Devanagari aren't read, so such events stay in review.
- A folder is judged as a whole; a mixed folder is held back when any file in it looks personal.

MIT licensed.
