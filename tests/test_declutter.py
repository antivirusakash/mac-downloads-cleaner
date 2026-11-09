"""Run: python3 -I -m unittest discover -s tests"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
import declutter  # noqa: E402
import owner  # noqa: E402
from make_fixture import EXPECTED, build  # noqa: E402


def jane():
    return owner.Owner(names=["Jane", "Doe"], handles=["janedoe"], brands=["AcmeApp"], work_hints=["acme"],
                       country="US", index_roots=[], full_name="Jane Doe")


class OwnerTest(unittest.TestCase):
    def test_name_at_word_start_only(self):
        # "Janet" counts too: a false "yours" only keeps a file in place.
        me = jane()
        self.assertEqual([me.in_name(n) for n in ("jane.gif", "Lease - Jane.pdf", "Janet.pdf", "Mary-jane.pdf",
                                                   "Prejane.pdf", "acme-ad.png")],
                         [True, True, True, True, False, True])

    def test_text_matches_whole_words_squashed_names_and_spaced_brands(self):
        me = jane()
        self.assertEqual([me.in_text(t) for t in ("Prepared by Jane", "JANEDOE", "Acme App launch", "Janet Jackson")],
                         [True, True, True, False])

    def test_country_pack_adds_local_id_terms(self):
        self.assertEqual([jane().id_name_re.search(n) is not None for n in ("W-2 2024.pdf", "aadhaar.pdf", "bank.pdf")],
                         [True, False, True])
        self.assertIn("Social Security Number", jane().id_terms)

    def test_spaced_brand(self):
        self.assertEqual(owner._spaced("AcmeApp"), "acme ?app")


class ProvenanceTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir)

    def write(self, name, data):
        p = os.path.join(self.dir, name)
        with open(p, "wb") as f:
            f.write(data)
        return p

    def test_reads_tool_and_kind(self):
        made = self.write("a.png", b"x" * 300 + b"claim_generator_info\xa1dnamegChatGPT trainedAlgorithmicMedia")
        edited = self.write("b.png", b"claim_generator_info\xa1dnameoAdobe Photoshop compositeWithTrainedAlgorithmicMedia")
        plain = self.write("c.png", b"\x89PNG plain")
        self.assertEqual([declutter.ai_provenance(p) for p in (made, edited, plain)],
                         [("made", "ChatGPT"), ("edited", "Adobe Photoshop"), None])

    def test_marker_near_end_of_big_file(self):
        p = self.write("d.webp", b"\0" * (3 << 17) + b"trainedAlgorithmicMedia" + b"\0" * 1000)
        self.assertEqual(declutter.ai_provenance(p), ("made", ""))


class DatesTest(unittest.TestCase):
    def test_reads_english_and_iso_dates(self):
        p = "/nowhere/quote.pdf"
        declutter.DOCTEXT[p] = "Trip from 10th Apr 2026 to April 14, 2026; invoice 2025-12-01"
        got = sorted(declutter.time.strftime("%Y-%m-%d", declutter.time.gmtime(t)) for t in declutter.doc_dates(p))
        self.assertEqual(got, ["2025-12-01", "2026-04-10", "2026-04-14"])


class EndToEndTest(unittest.TestCase):
    """Scan a made-up Downloads folder and check every file lands in its expected group."""

    @classmethod
    def setUpClass(cls):
        cls.dir = os.path.realpath(tempfile.mkdtemp())
        build(cls.dir)
        out = os.path.join(cls.dir, "out")
        subprocess.run([sys.executable, "-I", os.path.join(ROOT, "declutter.py"), "--config",
                        os.path.join(cls.dir, "config.json"), "--out", out, os.path.join(cls.dir, "Downloads")],
                       check=True, capture_output=True)
        cls.report = os.path.join(out, "report.json")
        items = json.load(open(cls.report))["items"]
        cls.got = {os.path.relpath(i["path"], cls.dir): (i["category"], i["tier"]) for i in items}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir)

    def test_every_file_is_placed(self):
        self.assertEqual(self.got, EXPECTED)

    def test_invariants_hold(self):
        r = subprocess.run([sys.executable, "-I", os.path.join(ROOT, "check.py"), self.report, "--config",
                            os.path.join(self.dir, "config.json")], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout)


if __name__ == "__main__":
    unittest.main()
