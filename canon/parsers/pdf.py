"""Minimal, dependency-free PDF text extraction.

Handles text-layer PDFs (EHR-printed notes, lab reports, e-faxes with a text
layer): FlateDecode streams, Tj/TJ/'/" operators, literal and hex strings.
Scanned image-only PDFs have no text layer; we report `needs_ocr` and the
pipeline hands the original PDF to the LLM extractor, which reads page images.
"""

from __future__ import annotations

import re
import zlib

STREAM_RX = re.compile(rb"<<(.*?)>>\s*stream\r?\n(.*?)\r?\nendstream", re.S)


def _unescape_literal(b: bytes) -> str:
    out = bytearray()
    i = 0
    while i < len(b):
        c = b[i]
        if c == 0x5C and i + 1 < len(b):  # backslash
            n = b[i + 1]
            mapping = {ord("n"): b"\n", ord("r"): b"\r", ord("t"): b"\t", ord("b"): b"\b", ord("f"): b"\f",
                       ord("("): b"(", ord(")"): b")", ord("\\"): b"\\"}
            if n in mapping:
                out += mapping[n]
                i += 2
                continue
            m = re.match(rb"[0-7]{1,3}", b[i + 1:i + 4])
            if m:
                out.append(int(m.group(0), 8) & 0xFF)
                i += 1 + len(m.group(0))
                continue
            if n in (0x0A, 0x0D):
                i += 2
                continue
            i += 1
            continue
        out.append(c)
        i += 1
    return out.decode("latin-1")


def _hex(b: bytes) -> str:
    h = re.sub(rb"\s", b"", b)
    if len(h) % 2:
        h += b"0"
    raw = bytes.fromhex(h.decode())
    if raw[:2] == b"\xfe\xff":
        return raw[2:].decode("utf-16-be", "ignore")
    return raw.decode("latin-1")


TOKEN_RX = re.compile(rb"\((?:\\.|[^\\()]|\((?:\\.|[^\\()])*\))*\)|<[0-9A-Fa-f\s]*>|\[|\]|[A-Za-z'\"*]+|-?\d*\.?\d+|/\S+",
                      re.S)


def _content_text(data: bytes) -> str:
    lines: list[str] = []
    cur: list[str] = []
    operands: list = []
    last_y = None
    in_array = False
    arr: list[str] = []

    def flush():
        if cur:
            lines.append("".join(cur))
            cur.clear()

    for tok in TOKEN_RX.findall(data):
        if tok.startswith(b"("):
            s = _unescape_literal(tok[1:-1])
            (arr if in_array else operands).append(s)
        elif tok.startswith(b"<") and tok != b"<<":
            s = _hex(tok[1:-1])
            (arr if in_array else operands).append(s)
        elif tok == b"[":
            in_array, arr = True, []
        elif tok == b"]":
            in_array = False
            operands.append(arr)
        elif re.fullmatch(rb"-?\d*\.?\d+", tok):
            if in_array:
                if float(tok) < -200:  # big negative kerning = word gap
                    arr.append(" ")
            else:
                operands.append(float(tok))
        else:
            op = tok.decode("latin-1")
            if op == "Tj" and operands and isinstance(operands[-1], str):
                cur.append(operands[-1])
            elif op == "TJ" and operands and isinstance(operands[-1], list):
                cur.append("".join(x for x in operands[-1] if isinstance(x, str)))
            elif op in ("'", '"') and operands and isinstance(operands[-1], str):
                flush()
                cur.append(operands[-1])
            elif op in ("Td", "TD") and len(operands) >= 2:
                if isinstance(operands[-1], float) and abs(operands[-1]) > 0.1:
                    flush()
                elif cur:
                    cur.append(" ")
            elif op == "Tm" and len(operands) >= 6:
                y = operands[-1]
                if last_y is not None and isinstance(y, float) and abs(y - last_y) > 0.1:
                    flush()
                last_y = y
            elif op in ("T*", "ET"):
                flush()
            operands = []
    flush()
    return "\n".join(l.rstrip() for l in lines)


def extract_text(pdf: bytes) -> tuple[str, dict]:
    if not pdf.startswith(b"%PDF"):
        raise ValueError("Not a PDF")
    chunks = []
    images = 0
    for m in STREAM_RX.finditer(pdf):
        head, body = m.group(1), m.group(2)
        if b"/Image" in head:
            images += 1
            continue
        if b"/FlateDecode" in head:
            try:
                body = zlib.decompress(body)
            except zlib.error:
                continue
        elif b"/Filter" in head:
            continue  # other filters (DCT, CCITT...) are images/fax bitmaps
        if b"BT" in body and (b"Tj" in body or b"TJ" in body):
            chunks.append(_content_text(body))
    text = "\n".join(c for c in chunks if c.strip())
    pages = len(re.findall(rb"/Type\s*/Page(?!s)", pdf))
    info = {"pages": pages, "chars": len(text), "image_streams": images,
            "needs_ocr": len(text.strip()) < 20}
    return text, info


def make_text_pdf(text: str) -> bytes:
    """Build a small single-page text PDF (used for samples and tests)."""
    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    ops = ["BT", "/F1 10 Tf", "12 TL", "50 780 Td"]
    for line in text.split("\n"):
        ops.append(f"({esc(line)}) Tj T*")
    ops.append("ET")
    content = zlib.compress("\n".join(ops).encode("latin-1"))
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(content)).encode() + b" /Filter /FlateDecode >>\nstream\n" + content +
        b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, o in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + o + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)
