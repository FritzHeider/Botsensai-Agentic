# Image fixtures

**Provenance: synthesized, then re-encoded by real encoders.** The pictures are
two numpy patterns (`alpha`, a disc and a bar over a diagonal gradient; `beta`,
unrelated stripes with a dark block), written out as PNG by a twenty-line
encoder. Everything else in this directory was produced from those two by
Apple ImageIO (`sips`) and ImageMagick (`magick`).

That split is the point. A decoder tested only against files this repository
wrote would prove nothing beyond self-consistency; every JPEG here came out of a
production encoder, with its own quantisation tables, chroma subsampling,
restart intervals and adaptive PNG filtering. No image was scraped, so none of
them carries a copyright or a person.

## The source images

```bash
# see tests/test_phash.py::_png_bytes for the encoder used to write these
alpha.png   256x256, colour type 2, filter 0 on every row
beta.png    256x256, an unrelated picture (Hamming distance 34 from alpha)
```

## Derived, and what each one is for

```bash
sips -s format jpeg -s formatOptions 85         alpha.png --out alpha_full.jpg
sips -s format jpeg -s formatOptions 35 -Z 96   alpha.png --out alpha_small.jpg
sips -s format jpeg -s formatOptions 70         beta.png  --out beta.jpg

magick alpha.png -quality 70 -define jpeg:restart-interval=2  alpha_restart.jpg
magick alpha.png -colorspace gray -quality 70                 alpha_gray.jpg
magick alpha.png -interlace Plane -quality 70                 alpha_progressive.jpg
magick alpha.png -sampling-factor 1x1 -quality 70             alpha_444.jpg

magick alpha.png -define png:compression-filter=5             alpha_filtered.png
magick alpha.png -define png:color-type=3 -colors 64          alpha_palette.png
magick alpha.png -colorspace gray -define png:color-type=0    alpha_grayscale.png
magick alpha.png -depth 16 -define png:bit-depth=16           alpha_16bit.png
```

| file | exercises |
| --- | --- |
| `alpha_full.jpg` | baseline JPEG, 4:2:0, luma quantisation 1, restart interval 16 |
| `alpha_small.jpg` | resize to 96px *and* re-encode at quality 35 — the acceptance case |
| `alpha_restart.jpg` | luma quantisation 10, restart every 2 MCUs |
| `alpha_gray.jpg` | single-component JPEG |
| `alpha_444.jpg` | 1x1 chroma sampling |
| `alpha_progressive.jpg` | must be **refused**: the DC-only decoder cannot read it |
| `alpha_filtered.png` | adaptive Sub/Up/Paeth scanline filters |
| `alpha_palette.png` | colour type 3 with a 64-entry PLTE |
| `alpha_grayscale.png` | colour type 0 |
| `alpha_16bit.png` | 16-bit channels |

The Average scanline filter is not represented here — ImageMagick would not emit
it — so `tests/test_phash.py` encodes all five filters itself rather than
leaving that path unexercised.
