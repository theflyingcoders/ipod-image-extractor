# iPod Photo Extractor

Recover photos from an iPod with one Python 3 script and no dependencies. It reads the iPod's `Photo Database`, picks the largest thumbnail of each photo, crops the black padding, and copies the full-resolution originals if iTunes stored them.

For iPods with photo support: iPod Photo (4G), iPod Video (5G/5.5G), iPod Classic and iPod Nano.

## Run it

### 1. Put the iPod in disk mode

1. Connect the iPod to your computer with its USB cable. If it shows up as a drive, it is already in disk mode. Go to step 2.
2. If not, set the **Hold** switch on, then off.
3. Hold **Select** (centre button) and **Menu** for about 6 seconds, until the Apple logo shows.
4. At once, hold **Select** and **Play/Pause** until the screen says **Disk Mode**.

### 2. Extract the photos

```bash
python3 ipod_photo_extractor.py                                          # auto-detect (macOS)
python3 ipod_photo_extractor.py /Volumes/IPOD -o ~/Desktop/ipod-photos   # explicit mount point
python3 ipod_photo_extractor.py ./backup -o out                          # a copied folder
```

The path can also be a copy of the iPod's contents: a folder that holds `Photos/`, or one that holds the `.ithmb` files and `Photo Database` directly.

With no path, the script uses the current folder if it contains `Photos`, `Photo Database` or `.ithmb` files. Otherwise it looks in `/Volumes` for a volume with a `Photos` or `iPod_Control` folder. That second step works on macOS only. On other systems, pass the path. If it finds several volumes, it prints one command for each.

## What you get

```
extracted_photos/
├── full_resolution/    originals, copied as they are
├── database_photos/    photo_00001_1067_720x480.png
└── carved_photos/      format_1067_720x480/F1067_1_00001.png
```

The script works in three phases:

1. **Originals.** Copies `jpg`, `jpeg`, `png`, `tif`, `tiff` and `bmp` files from `Photos/Full Resolution`.
2. **Photo Database.** Reads `Photos/Photo Database` (or `Photo Database` in the root, or `iPod_Control/Artwork/ArtworkDB`). It groups the thumbnails of each photo, decodes the largest one from the `.ithmb` files, and crops it to the image area stored in the database.
3. **Carving.** Runs only if phase 2 found no photos, or with `--carve-only`. It decodes every frame of every `.ithmb` file by its `F####` file-name prefix and skips blank frames.

## Options

| Flag | Effect |
|---|---|
| `-o, --output DIR` | Output folder. Default: `extracted_photos` |
| `--all-sizes` | Extract every thumbnail size, not only the largest |
| `--no-crop` | Keep the whole native frame, with its black bars |
| `--carve-only` | Skip the database and carve the `.ithmb` files |
| `--full-res-only` | Copy the full-resolution originals and stop |

## Supported formats

The `F####` prefix of an `.ithmb` file sets its format.

| Format | Size | Encoding |
|---|---|---|
| `1019` | 720×480 | UYVY 4:2:2, interlaced (TV-out) |
| `1067` | 720×480 | planar YCbCr 4:2:0 |
| `1081` | 640×480 | RGB565 |
| `3008` | 640×480 | BGR555 |
| `1024`, `1042` | 320×240 | RGB565 |
| `1013` | 220×176 | RGB565, big-endian |
| `1023` | 176×132 | RGB565, big-endian |
| `1015`, `1043` | 130×88 | RGB565 |

Smaller thumbnail and cover-art formats are listed in `PROFILES` in the script.

## Stuff to know

- **Formats are not all checked on real devices.** The table comes from the profile definitions in the script. If a photo has wrong colours or shifted rows, open an issue with your iPod model and the `F####` prefixes in `Photos/Thumbs`.
- **Cropping follows the database.** The crop uses the padding and size stored in the `Photo Database`. Tests cover it with synthetic data only. If a photo is cut off, run with `--no-crop` and open an issue.
- **Solid-colour frames are skipped.** The script treats them as empty slots, so a real all-black or all-white photo is lost.
- **Carving takes everything.** It decodes every `.ithmb` file under the source, including cover-art files.

## Tests

```bash
python3 -m unittest
```

The tests build small fake `.ithmb` files and a fake `Photo Database`. No photos are in the repo. Tested with Python 3.11.

## License

[MIT](LICENSE)
