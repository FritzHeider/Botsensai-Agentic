"""Decode image bytes to a luma array, with no image library.

Botsensai has no Pillow and is not going to acquire one: the whole reason a
perceptual hash is affordable here is that it costs a few hundred lines and no
new wheel. What it needs from a decoder is far less than what a decoder normally
provides — a small greyscale array is enough for a 32x32 DCT, and everything
about colour, alpha, ICC profiles and full-resolution reconstruction is waste.

Two formats, chosen because they are what the surfaces actually serve:

* **PNG** — full 8/16-bit decode via `zlib` plus the five scanline filters.
  Non-interlaced only; Adam7 is rare enough on social media to refuse.
* **JPEG** — *DC coefficients only*. This is the trick that makes a pure-Python
  JPEG affordable. Each 8x8 block's DC term is, by construction, that block's
  mean brightness, so decoding DC alone yields the image at exactly 1/8 scale
  with no inverse DCT, no upsampling and no colour conversion. A perceptual hash
  downsamples to 32x32 anyway, so the discarded detail was never going to reach
  the hash.

  Both baseline and progressive are read, and progressive is the *cheaper* of
  the two here — it puts the DC coefficients in their own leading scan, so there
  are no AC coefficients to decode and throw away. That is not an optimisation
  note: measured 2026-08-04, every image `pbs.twimg.com` served was progressive,
  so a baseline-only decoder produces zero coverage on X. Successive-approximation
  refinement scans are skipped, which costs the DC values their bottom `Al` bits
  (one, in practice) and nothing the hash can see.

Anything else — GIF, WebP, video — raises `UnsupportedImage`. That is a decoding
refusal, not a claim about the image, and the caller must record it as such.
"""

from __future__ import annotations

import zlib
from dataclasses import dataclass, field

import numpy as np

__all__ = ["UnsupportedImage", "decode_luma", "sniff_format"]

#: Luma weights (ITU-R BT.601), the same basis JPEG itself uses for its Y plane,
#: so a PNG and a JPEG of the same picture land in the same place.
_LUMA = np.array([0.299, 0.587, 0.114], dtype=np.float32)

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

#: Refusing an oversized image is a budget decision, not a correctness one: this
#: decoder is pure Python and JPEG cost scales with entropy-coded bits, not just
#: pixels. Measured 2026-08-04 on this machine: a 96x96 JPEG in 1.2ms, 256x256 in
#: 9.2ms, and a noisy 1200x1200 in 863ms — the worst case, because noise is what
#: makes the Huffman stream long. PNG is an order of magnitude cheaper (23ms at
#: 1200x1200) since `zlib` does the work in C. The cap sits where a single
#: pathological image still cannot eat a sweep; the real saving is fetching
#: thumbnails, which keeps almost everything in the first bracket.
DEFAULT_MAX_PIXELS = 4_000_000


class UnsupportedImage(Exception):
    """The bytes are not an image this decoder handles.

    Distinct from a transport failure on purpose. An unsupported image will
    still be unsupported next sweep, so the caller may cache it; a timeout will
    not, and must not be cached.
    """


def sniff_format(data: bytes) -> str:
    """Name the container from its magic bytes, without trusting Content-Type."""
    if data.startswith(_PNG_MAGIC):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data.startswith(b"GIF8"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return "unknown"


def decode_luma(data: bytes, *, max_pixels: int = DEFAULT_MAX_PIXELS) -> np.ndarray:
    """Return a 2-D float32 luma array in [0, 255].

    For JPEG the array is at 1/8 of the nominal resolution (one sample per 8x8
    block); for PNG it is full resolution. Both are fine for hashing, which
    resamples to a fixed grid regardless.
    """
    kind = sniff_format(data)
    if kind == "png":
        return _decode_png(data, max_pixels)
    if kind == "jpeg":
        return _decode_jpeg_dc(data, max_pixels)
    raise UnsupportedImage(f"unsupported image format: {kind}")


# --------------------------------------------------------------------------- #
# PNG
# --------------------------------------------------------------------------- #


@dataclass
class _PngHeader:
    width: int
    height: int
    depth: int
    colour: int
    interlace: int

    @property
    def channels(self) -> int:
        return {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[self.colour]


def _png_chunks(data: bytes) -> tuple[_PngHeader, bytes, bytes]:
    """Walk the chunk stream, returning the header, the palette and the IDATs."""
    header: _PngHeader | None = None
    palette = b""
    idat = bytearray()
    pos = len(_PNG_MAGIC)
    while pos + 8 <= len(data):
        length = int.from_bytes(data[pos : pos + 4], "big")
        name = data[pos + 4 : pos + 8]
        body = data[pos + 8 : pos + 8 + length]
        pos += 12 + length
        if name == b"IHDR":
            header = _png_header(body)
        elif name == b"PLTE":
            palette = body
        elif name == b"IDAT":
            idat += body
        elif name == b"IEND":
            break
    if header is None:
        raise UnsupportedImage("png without IHDR")
    return header, palette, bytes(idat)


def _png_header(body: bytes) -> _PngHeader:
    if len(body) < 13:
        raise UnsupportedImage("truncated IHDR")
    header = _PngHeader(
        width=int.from_bytes(body[0:4], "big"),
        height=int.from_bytes(body[4:8], "big"),
        depth=body[8],
        colour=body[9],
        interlace=body[12],
    )
    if header.colour not in (0, 2, 3, 4, 6):
        raise UnsupportedImage(f"png colour type {header.colour}")
    if header.depth not in (8, 16):
        # Sub-byte depths need bit unpacking that social imagery never uses.
        raise UnsupportedImage(f"png bit depth {header.depth}")
    if header.interlace:
        raise UnsupportedImage("interlaced png")
    if header.width <= 0 or header.height <= 0:
        raise UnsupportedImage("png with empty dimensions")
    return header


def _decode_png(data: bytes, max_pixels: int) -> np.ndarray:
    header, palette, idat = _png_chunks(data)
    _guard_pixels(header.width, header.height, max_pixels)
    if not idat:
        raise UnsupportedImage("png without image data")
    try:
        raw = zlib.decompress(idat)
    except zlib.error as exc:  # pragma: no cover - corrupt bytes
        raise UnsupportedImage(f"png inflate failed: {exc}") from exc

    stride = header.width * header.channels * (header.depth // 8)
    if len(raw) < (stride + 1) * header.height:
        raise UnsupportedImage("truncated png scanlines")
    pixels = _png_unfilter(raw, stride, header.height, header.channels * (header.depth // 8))
    samples = pixels.reshape(header.height, header.width, header.channels * (header.depth // 8))
    if header.depth == 16:
        # High byte only: the hash cannot see the low one.
        samples = samples[:, :, 0::2]
    return _png_to_luma(samples.astype(np.float32), header, palette)


def _png_unfilter(raw: bytes, stride: int, height: int, bpp: int) -> np.ndarray:
    """Reverse the per-scanline filters. Sequential by definition."""
    out = bytearray(stride * height)
    prior = bytearray(stride)
    pos = 0
    for row in range(height):
        ftype = raw[pos]
        line = bytearray(raw[pos + 1 : pos + 1 + stride])
        pos += 1 + stride
        if ftype == 1:
            _unfilter_sub(line, bpp)
        elif ftype == 2:
            _unfilter_up(line, prior)
        elif ftype == 3:
            _unfilter_average(line, prior, bpp)
        elif ftype == 4:
            _unfilter_paeth(line, prior, bpp)
        elif ftype != 0:
            raise UnsupportedImage(f"png filter type {ftype}")
        out[row * stride : (row + 1) * stride] = line
        prior = line
    return np.frombuffer(bytes(out), dtype=np.uint8)


def _unfilter_sub(line: bytearray, bpp: int) -> None:
    for i in range(bpp, len(line)):
        line[i] = (line[i] + line[i - bpp]) & 0xFF


def _unfilter_up(line: bytearray, prior: bytearray) -> None:
    # The only filter with no left-neighbour dependency, so numpy can do it whole.
    cur = np.frombuffer(bytes(line), dtype=np.uint8)
    up = np.frombuffer(bytes(prior), dtype=np.uint8)
    line[:] = (cur + up).tobytes()


def _unfilter_average(line: bytearray, prior: bytearray, bpp: int) -> None:
    for i in range(len(line)):
        left = line[i - bpp] if i >= bpp else 0
        line[i] = (line[i] + ((left + prior[i]) >> 1)) & 0xFF


def _unfilter_paeth(line: bytearray, prior: bytearray, bpp: int) -> None:
    for i in range(len(line)):
        left = line[i - bpp] if i >= bpp else 0
        upleft = prior[i - bpp] if i >= bpp else 0
        line[i] = (line[i] + _paeth(left, prior[i], upleft)) & 0xFF


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def _png_to_luma(samples: np.ndarray, header: _PngHeader, palette: bytes) -> np.ndarray:
    if header.colour == 3:
        return _palette_to_luma(samples[:, :, 0], palette)
    if header.colour in (0, 4):
        return samples[:, :, 0]
    return samples[:, :, :3] @ _LUMA


def _palette_to_luma(indices: np.ndarray, palette: bytes) -> np.ndarray:
    if not palette:
        raise UnsupportedImage("palette png without PLTE")
    table = np.frombuffer(palette, dtype=np.uint8).reshape(-1, 3).astype(np.float32) @ _LUMA
    clipped = np.clip(indices.astype(np.int32), 0, len(table) - 1)
    return table[clipped]


# --------------------------------------------------------------------------- #
# JPEG (DC only)
# --------------------------------------------------------------------------- #


@dataclass
class _Component:
    ident: int
    h: int
    v: int
    quant: int
    dc_table: int = 0
    ac_table: int = 0
    pred: int = 0


@dataclass
class _Jpeg:
    width: int = 0
    height: int = 0
    restart_interval: int = 0
    progressive: bool = False
    #: Successive-approximation shift of the DC scan; refinement scans are skipped.
    point_transform: int = 0
    components: list[_Component] = field(default_factory=list)
    quant: dict[int, np.ndarray] = field(default_factory=dict)
    huff_dc: dict[int, dict[tuple[int, int], int]] = field(default_factory=dict)
    huff_ac: dict[int, dict[tuple[int, int], int]] = field(default_factory=dict)


def _decode_jpeg_dc(data: bytes, max_pixels: int) -> np.ndarray:
    jpeg, scan_start = _parse_jpeg_headers(data, max_pixels)
    if not jpeg.components:
        raise UnsupportedImage("jpeg without frame components")
    return _decode_jpeg_scan(jpeg, data, scan_start)


def _parse_jpeg_headers(data: bytes, max_pixels: int) -> tuple[_Jpeg, int]:
    """Read markers up to (and including) SOS. Returns the entropy-data offset."""
    jpeg = _Jpeg()
    pos = 2
    while pos + 4 <= len(data):
        if data[pos] != 0xFF:
            raise UnsupportedImage("jpeg marker misalignment")
        marker = data[pos + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            pos += 2
            continue
        length = int.from_bytes(data[pos + 2 : pos + 4], "big")
        body = data[pos + 4 : pos + 2 + length]
        pos += 2 + length
        if marker == 0xDA:
            _read_scan_header(jpeg, body)
            return jpeg, pos
        _read_jpeg_segment(jpeg, marker, body, max_pixels)
    raise UnsupportedImage("jpeg without a scan")


def _read_jpeg_segment(jpeg: _Jpeg, marker: int, body: bytes, max_pixels: int) -> None:
    if marker in (0xC0, 0xC1, 0xC2):
        # SOF0/SOF1 baseline and extended sequential, SOF2 progressive. All three
        # describe the frame identically; only the scan structure differs.
        jpeg.progressive = marker == 0xC2
        _read_frame_header(jpeg, body, max_pixels)
    elif 0xC3 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
        # Lossless, hierarchical and arithmetic-coded frames. Vanishingly rare on
        # the web and each needs a different entropy decoder.
        raise UnsupportedImage(f"unsupported jpeg frame (SOF marker 0x{marker:02x})")
    elif marker == 0xC4:
        _read_huffman_tables(jpeg, body)
    elif marker == 0xDB:
        _read_quant_tables(jpeg, body)
    elif marker == 0xDD:
        jpeg.restart_interval = int.from_bytes(body[:2], "big")


def _read_frame_header(jpeg: _Jpeg, body: bytes, max_pixels: int) -> None:
    if len(body) < 6:
        raise UnsupportedImage("truncated SOF")
    if body[0] != 8:
        raise UnsupportedImage(f"jpeg sample precision {body[0]}")
    jpeg.height = int.from_bytes(body[1:3], "big")
    jpeg.width = int.from_bytes(body[3:5], "big")
    _guard_pixels(jpeg.width, jpeg.height, max_pixels)
    for i in range(body[5]):
        off = 6 + i * 3
        jpeg.components.append(
            _Component(
                ident=body[off],
                h=max(1, body[off + 1] >> 4),
                v=max(1, body[off + 1] & 0x0F),
                quant=body[off + 2],
            )
        )


def _read_scan_header(jpeg: _Jpeg, body: bytes) -> None:
    count = body[0]
    by_id = {c.ident: c for c in jpeg.components}
    for i in range(count):
        ident, tables = body[1 + i * 2], body[2 + i * 2]
        comp = by_id.get(ident)
        if comp is None:
            raise UnsupportedImage("scan names an unknown component")
        comp.dc_table = tables >> 4
        comp.ac_table = tables & 0x0F
    if count != len(jpeg.components):
        # An interleaved scan over every component is what both a baseline image
        # and a progressive image's leading DC scan look like. A partial scan is
        # a per-component AC pass, which carries nothing this decoder wants.
        raise UnsupportedImage("scan does not cover every component")
    _read_spectral_selection(jpeg, body, count)


def _read_spectral_selection(jpeg: _Jpeg, body: bytes, count: int) -> None:
    """Ss/Se/AhAl. Only meaningful for progressive, where it names the scan."""
    offset = 1 + count * 2
    if len(body) < offset + 3:
        raise UnsupportedImage("truncated SOS")
    start, approximation = body[offset], body[offset + 2]
    jpeg.point_transform = approximation & 0x0F
    if not jpeg.progressive:
        return
    if start != 0 or (approximation >> 4) != 0:
        # The first scan of a progressive image is its DC scan by definition, so
        # anything else here means the file is ordered in a way we cannot read
        # without decoding the AC passes we exist to avoid.
        raise UnsupportedImage("progressive jpeg without a leading DC scan")


def _read_quant_tables(jpeg: _Jpeg, body: bytes) -> None:
    pos = 0
    while pos < len(body):
        precision, ident = body[pos] >> 4, body[pos] & 0x0F
        pos += 1
        size = 64 * (2 if precision else 1)
        chunk = body[pos : pos + size]
        pos += size
        dtype = ">u2" if precision else "u1"
        jpeg.quant[ident] = np.frombuffer(chunk, dtype=dtype).astype(np.float32)


def _read_huffman_tables(jpeg: _Jpeg, body: bytes) -> None:
    pos = 0
    while pos + 17 <= len(body):
        table_class, ident = body[pos] >> 4, body[pos] & 0x0F
        counts = body[pos + 1 : pos + 17]
        pos += 17
        total = sum(counts)
        symbols = body[pos : pos + total]
        pos += total
        table = _build_huffman(counts, symbols)
        target = jpeg.huff_ac if table_class else jpeg.huff_dc
        target[ident] = table


def _build_huffman(counts: bytes, symbols: bytes) -> dict[tuple[int, int], int]:
    """Canonical JPEG Huffman: codes assigned in length order, keyed (len, code)."""
    table: dict[tuple[int, int], int] = {}
    code = 0
    k = 0
    for length in range(1, 17):
        for _ in range(counts[length - 1]):
            table[(length, code)] = symbols[k]
            code += 1
            k += 1
        code <<= 1
    return table


class _BitReader:
    """MSB-first bit reader over JPEG entropy-coded data.

    Handles the two things that make that stream not a plain byte stream: 0xFF00
    is a stuffed 0xFF, and any other 0xFFxx is a marker that ends the segment.
    """

    def __init__(self, data: bytes, pos: int) -> None:
        self._data = data
        self._pos = pos
        self._buf = 0
        self._bits = 0

    def read_bit(self) -> int:
        if self._bits == 0:
            self._buf = self._next_byte()
            self._bits = 8
        self._bits -= 1
        return (self._buf >> self._bits) & 1

    def _next_byte(self) -> int:
        if self._pos >= len(self._data):
            raise UnsupportedImage("jpeg entropy data ran out")
        byte = self._data[self._pos]
        self._pos += 1
        if byte != 0xFF:
            return byte
        following = self._data[self._pos] if self._pos < len(self._data) else 0xD9
        if following == 0x00:
            self._pos += 1
            return 0xFF
        raise UnsupportedImage("jpeg marker inside entropy data")

    def receive(self, count: int) -> int:
        value = 0
        for _ in range(count):
            value = (value << 1) | self.read_bit()
        return value

    def decode(self, table: dict[tuple[int, int], int]) -> int:
        code = 0
        for length in range(1, 17):
            code = (code << 1) | self.read_bit()
            symbol = table.get((length, code))
            if symbol is not None:
                return symbol
        raise UnsupportedImage("jpeg huffman code not in table")

    def restart(self) -> None:
        """Byte-align and step over the RSTn marker."""
        self._bits = 0
        while self._pos + 1 < len(self._data):
            if self._data[self._pos] == 0xFF and 0xD0 <= self._data[self._pos + 1] <= 0xD7:
                self._pos += 2
                return
            self._pos += 1
        raise UnsupportedImage("jpeg restart marker missing")


def _extend(value: int, size: int) -> int:
    """JPEG's signed representation: the top bit clear means a negative value."""
    if size == 0:
        return 0
    return value if value >= (1 << (size - 1)) else value - (1 << size) + 1


def _decode_block_dc(reader: _BitReader, jpeg: _Jpeg, comp: _Component) -> int:
    """Decode one block's DC term and step the reader past its AC terms.

    In a progressive DC scan there are no AC terms to step over — that is the
    whole scan — so the skip is baseline-only.
    """
    size = reader.decode(jpeg.huff_dc.get(comp.dc_table, {}))
    comp.pred += _extend(reader.receive(size), size)
    if not jpeg.progressive:
        _skip_ac(reader, jpeg.huff_ac.get(comp.ac_table, {}))
    return comp.pred << jpeg.point_transform


def _skip_ac(reader: _BitReader, table: dict[tuple[int, int], int]) -> None:
    """Consume the 63 AC coefficients without keeping them.

    They still have to be *decoded* — the stream is not seekable — but nothing
    is stored, which is what makes this decoder cheap.
    """
    k = 1
    while k <= 63:
        symbol = reader.decode(table)
        run, size = symbol >> 4, symbol & 0x0F
        if size == 0:
            if run != 15:
                return  # end of block
            k += 16
            continue
        k += run + 1
        reader.receive(size)


def _decode_jpeg_scan(jpeg: _Jpeg, data: bytes, pos: int) -> np.ndarray:
    hmax = max(c.h for c in jpeg.components)
    vmax = max(c.v for c in jpeg.components)
    mcus_x = -(-jpeg.width // (8 * hmax))
    mcus_y = -(-jpeg.height // (8 * vmax))
    luma = jpeg.components[0]
    plane = np.zeros((mcus_y * luma.v, mcus_x * luma.h), dtype=np.float32)
    scale = float(jpeg.quant.get(luma.quant, np.ones(64, dtype=np.float32))[0])

    reader = _BitReader(data, pos)
    for index in range(mcus_x * mcus_y):
        if jpeg.restart_interval and index and index % jpeg.restart_interval == 0:
            reader.restart()
            for comp in jpeg.components:
                comp.pred = 0
        _decode_one_mcu(reader, jpeg, luma, plane, divmod(index, mcus_x))

    # DC * quant / 8 is the block mean in JPEG's level-shifted space; +128 puts
    # it back into 0..255.
    return np.clip(plane * (scale / 8.0) + 128.0, 0.0, 255.0)


def _decode_one_mcu(
    reader: _BitReader,
    jpeg: _Jpeg,
    luma: _Component,
    plane: np.ndarray,
    cell: tuple[int, int],
) -> None:
    mcu_row, mcu_col = cell
    for comp in jpeg.components:
        for block in range(comp.h * comp.v):
            dc = _decode_block_dc(reader, jpeg, comp)
            if comp is not luma:
                continue
            row = mcu_row * comp.v + block // comp.h
            col = mcu_col * comp.h + block % comp.h
            plane[row, col] = dc


def _guard_pixels(width: int, height: int, max_pixels: int) -> None:
    if width <= 0 or height <= 0:
        raise UnsupportedImage("image with empty dimensions")
    if width * height > max_pixels:
        raise UnsupportedImage(f"image {width}x{height} exceeds {max_pixels} pixel budget")
