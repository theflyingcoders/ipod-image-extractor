import copy
import struct
import tempfile
import unittest
from pathlib import Path

import ipod_photo_extractor as ipx

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def frame_565(w, h):
    """RGB565 little-endian gradient, so the frame is not blank."""
    return b"".join(struct.pack("<H", (x * 7 + y * 13) & 0xFFFF) for y in range(h) for x in range(w))


def photo_db(fmt, offset, size, vpad, hpad, h, w):
    """Smallest Photo Database the scanner accepts: one mhii holding one mhni."""
    mhni = b"mhni" + struct.pack("<IIIIII", 36, 36, 0, fmt, offset, size) + struct.pack("<hhHH", vpad, hpad, h, w)
    return b"mhfd" + b"mhii" + struct.pack("<II", 12, 12 + len(mhni)) + mhni


class ExtractorTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

    def test_mhni_rectangle_is_padding_plus_size(self):
        # 30x30 image centred in a 42x30 frame: 6px padding left and right
        photos = ipx.scan_photo_database_signatures(photo_db(1009, 0, 2520, 0, 6, 30, 30))
        rep = photos[0][0]
        self.assertEqual((rep["x1"], rep["y1"], rep["x2"], rep["y2"]), (6, 0, 36, 30))

    def test_database_photo_is_cropped(self):
        thumbs = self.tmp / "Photos" / "Thumbs"
        thumbs.mkdir(parents=True)
        (thumbs / "F1009_1.ithmb").write_bytes(frame_565(42, 30))
        photos = ipx.scan_photo_database_signatures(photo_db(1009, 0, 2520, 0, 6, 30, 30))

        saved = ipx.extract_from_database(photos, [thumbs], self.tmp / "out")

        png = self.tmp / "out" / "database_photos" / "photo_00001_1009_30x30.png"
        self.assertEqual(saved, 1)
        self.assertTrue(png.read_bytes().startswith(PNG_MAGIC))

    def test_unusable_records_do_not_crash(self):
        photos = ipx.scan_photo_database_signatures(photo_db(1009, 0, 2520, 0, 6, 30, 30))
        nameless = copy.deepcopy(photos)
        nameless[0][0]["filename"] = None
        missing = copy.deepcopy(photos)
        missing[0][0]["filename"] = "F9999_1.ithmb"

        self.assertEqual(ipx.extract_from_database(nameless + missing, [self.tmp], self.tmp / "out"), 0)

    def test_full_resolution_lists_folder_once_and_skips_stubs(self):
        full = self.tmp / "Photos" / "Full_Resolution" / "2007"
        full.mkdir(parents=True)
        for name in ("a.jpg", "._a.jpg", "B.PNG", "notes.txt"):
            (full / name).write_bytes(b"x")

        saved = ipx.extract_full_resolution(self.tmp, self.tmp / "out")

        self.assertEqual(saved, 2)
        self.assertEqual(sorted(p.name for p in (self.tmp / "out" / "full_resolution").iterdir()), ["B.PNG", "a.jpg"])

    def test_carving_skips_blank_slots(self):
        ithmb = self.tmp / "F1009_1.ithmb"
        ithmb.write_bytes(frame_565(42, 30) + bytes(2520))

        self.assertEqual(ipx.carve_ithmb_file(ithmb, self.tmp / "out"), 1)

    def test_sort_key_survives_oddly_named_files(self):
        files = [Path("junk.ithmb"), Path("F1019_1.ithmb")]
        files.sort(key=ipx.ithmb_priority, reverse=True)
        self.assertEqual(files[0].name, "F1019_1.ithmb")


if __name__ == "__main__":
    unittest.main()
