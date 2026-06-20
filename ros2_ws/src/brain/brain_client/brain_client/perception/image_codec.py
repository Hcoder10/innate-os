"""Pure image/video packaging — no ROS, no OpenCV, no temp files.

Camera frames already arrive JPEG-compressed (sensor_msgs/CompressedImage), so the
brain forwards those bytes untouched instead of decoding and re-encoding them. This
module base64-wraps a single JPEG and muxes a sequence of JPEGs into an MJPEG/AVI
clip by hand — an AVI is just the JPEG frames in a RIFF wrapper, so no re-encode is
needed. Stays unit-testable without a ROS runtime.
"""

from __future__ import annotations

import base64
import struct


def b64encode_jpeg(jpeg: bytes | None) -> str | None:
    """Base64-encode already-compressed JPEG bytes. Returns None if empty."""
    if not jpeg:
        return None
    return base64.b64encode(jpeg).decode("utf-8")


def mux_mjpeg_avi_b64(frames: list[bytes], fps: float) -> str | None:
    """Mux already-JPEG ``frames`` into an MJPEG/AVI clip, base64-encoded.

    No re-encoding: each frame's JPEG bytes are written verbatim into the AVI
    ``movi`` list. Returns None if there are no usable frames or the first frame's
    dimensions can't be read.
    """
    frames = [f for f in frames if f]
    if not frames:
        return None
    dims = _jpeg_dimensions(frames[0])
    if dims is None or dims[0] == 0 or dims[1] == 0:
        return None
    return base64.b64encode(_build_avi(frames, dims[0], dims[1], fps)).decode("utf-8")


def _jpeg_dimensions(jpeg: bytes) -> tuple[int, int] | None:
    """Read (width, height) from a JPEG's SOF marker without decoding pixels."""
    if len(jpeg) < 2 or jpeg[0] != 0xFF or jpeg[1] != 0xD8:
        return None
    i, n = 2, len(jpeg)
    while i < n:
        if jpeg[i] != 0xFF:
            i += 1
            continue
        while i < n and jpeg[i] == 0xFF:  # skip fill bytes before the marker
            i += 1
        if i >= n:
            break
        marker = jpeg[i]
        i += 1
        # Standalone markers (SOI/EOI/TEM/RSTn) carry no length field.
        if marker in (0xD8, 0xD9, 0x01) or 0xD0 <= marker <= 0xD7:
            continue
        if i + 1 >= n:
            break
        # SOF markers (0xC0-0xCF, except DHT/JPG/DAC) hold the frame size.
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            if i + 6 >= n:
                break
            height = (jpeg[i + 3] << 8) | jpeg[i + 4]
            width = (jpeg[i + 5] << 8) | jpeg[i + 6]
            return (width, height)
        i += (jpeg[i] << 8) | jpeg[i + 1]  # skip this marker segment
    return None


def _chunk(fourcc: bytes, data: bytes) -> bytes:
    """A RIFF chunk: fourcc + little-endian size + data, padded to an even length."""
    chunk = fourcc + struct.pack("<I", len(data)) + data
    return chunk + b"\x00" if len(data) & 1 else chunk


def _list(list_type: bytes, payload: bytes) -> bytes:
    return b"LIST" + struct.pack("<I", 4 + len(payload)) + list_type + payload


def _build_avi(frames: list[bytes], width: int, height: int, fps: float) -> bytes:
    fps = max(float(fps), 1.0)
    micros_per_frame = int(round(1_000_000 / fps))
    rate = max(int(round(fps)), 1)
    num = len(frames)

    avih = struct.pack(
        "<14I",
        micros_per_frame,  # dwMicroSecPerFrame
        0,  # dwMaxBytesPerSec
        0,  # dwPaddingGranularity
        0x10,  # dwFlags = AVIF_HASINDEX
        num,  # dwTotalFrames
        0,  # dwInitialFrames
        1,  # dwStreams
        0,  # dwSuggestedBufferSize
        width,
        height,
        0,
        0,
        0,
        0,  # dwReserved[4]
    )
    strh = struct.pack(
        "<4s4sIHH8I4H",
        b"vids",
        b"MJPG",
        0,  # dwFlags
        0,  # wPriority
        0,  # wLanguage
        0,  # dwInitialFrames
        1,  # dwScale
        rate,  # dwRate (rate/scale = fps)
        0,  # dwStart
        num,  # dwLength
        0,  # dwSuggestedBufferSize
        0,  # dwQuality
        0,  # dwSampleSize
        0,
        0,
        width,
        height,  # rcFrame: left, top, right, bottom
    )
    strf = struct.pack(
        "<IiiHH4sIiiII",
        40,  # biSize
        width,  # biWidth
        height,  # biHeight
        1,  # biPlanes
        24,  # biBitCount
        b"MJPG",  # biCompression
        width * height * 3,  # biSizeImage
        0,
        0,  # biXPelsPerMeter, biYPelsPerMeter
        0,
        0,  # biClrUsed, biClrImportant
    )
    hdrl = _list(b"hdrl", _chunk(b"avih", avih) + _list(b"strl", _chunk(b"strh", strh) + _chunk(b"strf", strf)))

    movi_data = b""
    index = b""
    offset = 4  # first '00dc' chunk header sits 4 bytes past the 'movi' fourcc
    for frame in frames:
        chunk = _chunk(b"00dc", frame)
        index += struct.pack("<4sIII", b"00dc", 0x10, offset, len(frame))  # id, KEYFRAME, offset, size
        movi_data += chunk
        offset += len(chunk)
    movi = _list(b"movi", movi_data)
    idx1 = _chunk(b"idx1", index)

    body = b"AVI " + hdrl + movi + idx1
    return b"RIFF" + struct.pack("<I", len(body)) + body
