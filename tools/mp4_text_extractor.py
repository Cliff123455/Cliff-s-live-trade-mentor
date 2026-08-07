#!/usr/bin/env python3
"""
mp4_text_extractor.py
=====================

Extract text messages (with dates) from an MP4 **screen recording**.

WHAT THIS IS FOR
----------------
You have a video (usually a phone screen recording) of a text-message /
chat conversation. You want a clean, dated list of every message that
appears in the video. This script does that:

    1. Samples frames out of the MP4 at a steady interval.
    2. Skips near-duplicate frames (a scrolling chat shows the same
       messages over and over -- we only OCR the ones that changed).
    3. Runs OCR (optical character recognition) on each unique frame.
    4. Pulls out message lines and any date / timestamp lines it can find.
    5. De-duplicates the messages across the whole video.
    6. Writes a "package" of results: a readable .txt transcript, a
       structured .json file, and a .csv you can open in Excel.

HAND-OFF NOTE (read me first)
-----------------------------
This is a self-contained CLI tool. To run it you need two things
installed:

    * Python packages:   pip install opencv-python pytesseract
    * The Tesseract OCR engine (the actual OCR program):
        - Windows : https://github.com/UB-Mannheim/tesseract/wiki
                    (then, if it isn't on PATH, pass --tesseract "C:\\Program Files\\Tesseract-OCR\\tesseract.exe")
        - macOS   : brew install tesseract
        - Linux   : sudo apt-get install tesseract-ocr

USAGE
-----
    python mp4_text_extractor.py path/to/video.mp4
    python mp4_text_extractor.py chat.mp4 --outdir results --fps 2
    python mp4_text_extractor.py chat.mp4 --tesseract "C:\\Program Files\\Tesseract-OCR\\tesseract.exe"

Point it at a whole folder of MP4s to process each file:

    python mp4_text_extractor.py path/to/folder --outdir results

OUTPUT
------
For each video "chat.mp4" you get, inside --outdir:

    chat.txt    <- human-readable transcript, one message per block
    chat.json   <- structured data (messages, dates, source frame time)
    chat.csv    <- spreadsheet-friendly (date, time, message)

It is intentionally forgiving: if OCR isn't perfect (it never is), you
still get every line it found so you can eyeball / clean up afterward.
"""

from __future__ import annotations

import argparse
import csv
import datetime as _dt
import json
import re
import sys
from pathlib import Path
from typing import Iterable

# ---- Third-party imports are done lazily so we can print a friendly
# ---- message instead of a raw traceback if they're missing. ----------
try:
    import cv2  # opencv-python
except ImportError:  # pragma: no cover - env dependent
    cv2 = None

try:
    import pytesseract
except ImportError:  # pragma: no cover - env dependent
    pytesseract = None


# ---------------------------------------------------------------------------
# Date / time detection
# ---------------------------------------------------------------------------
# Chat apps show dates and timestamps in lots of shapes. We look for the
# common ones. These patterns are deliberately broad -- better to catch a
# few extra "date-ish" lines than to miss real ones.

_MONTHS = (
    r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
    r"[a-z]*"
)

DATE_PATTERNS = [
    # 2024-01-31 / 2024/01/31
    re.compile(r"\b\d{4}[-/]\d{1,2}[-/]\d{1,2}\b"),
    # 31/01/2024 / 1-31-24 / 01.31.2024
    re.compile(r"\b\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}\b"),
    # January 31, 2024 / Jan 31 / 31 January 2024
    re.compile(rf"\b{_MONTHS}\.?\s+\d{{1,2}}(?:,?\s+\d{{2,4}})?\b", re.IGNORECASE),
    re.compile(rf"\b\d{{1,2}}\s+{_MONTHS}\.?(?:\s+\d{{2,4}})?\b", re.IGNORECASE),
    # Weekday labels that chat apps use as date separators
    re.compile(r"\b(?:Today|Yesterday|Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*\b", re.IGNORECASE),
]

# 9:41, 09:41, 9:41 AM, 21:07
TIME_PATTERN = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\s*(?:[AaPp]\.?[Mm]\.?)?\b")


def find_dates(line: str) -> list[str]:
    """Return every date-looking substring in a line."""
    found: list[str] = []
    for pat in DATE_PATTERNS:
        found.extend(m.group(0).strip() for m in pat.finditer(line))
    return found


def find_time(line: str) -> str | None:
    m = TIME_PATTERN.search(line)
    return m.group(0).strip() if m else None


def looks_like_only_date_or_time(line: str) -> bool:
    """True if the line is basically just a date/time separator (no message)."""
    stripped = line.strip()
    if not stripped:
        return True
    # Remove all dates and times, see if anything meaningful is left.
    residue = TIME_PATTERN.sub("", stripped)
    for pat in DATE_PATTERNS:
        residue = pat.sub("", residue)
    residue = re.sub(r"[\s\-•·|,.]+", "", residue)
    return len(residue) <= 1


# ---------------------------------------------------------------------------
# Frame sampling + de-duplication
# ---------------------------------------------------------------------------
def _frame_signature(gray) -> "any":
    """A small, cheap fingerprint of a frame so we can tell if it changed.

    We shrink the frame to 16x16 and threshold it -- this is a classic
    'average hash'. Two frames with the same hash are visually the same,
    which for a chat recording means 'nothing scrolled / nothing new'.
    """
    small = cv2.resize(gray, (16, 16), interpolation=cv2.INTER_AREA)
    avg = small.mean()
    bits = (small > avg).flatten()
    return bits


def _hamming(a, b) -> int:
    return int((a != b).sum())


def iter_unique_frames(
    video_path: Path,
    target_fps: float,
    change_threshold: int,
) -> Iterable[tuple[float, "any"]]:
    """Yield (timestamp_seconds, frame) for frames that differ from the last.

    target_fps: how many frames per second to *inspect* (not the video's
        real fps). 1-2 is plenty for a chat recording and keeps OCR fast.
    change_threshold: how many hash bits must differ before we treat the
        frame as 'new'. Higher = fewer frames OCR'd.
    """
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise RuntimeError(f"Could not open video: {video_path}")

    native_fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    if native_fps <= 0:
        native_fps = 30.0
    step = max(1, int(round(native_fps / max(target_fps, 0.01))))

    frame_idx = 0
    last_sig = None
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            if frame_idx % step == 0:
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                sig = _frame_signature(gray)
                if last_sig is None or _hamming(sig, last_sig) >= change_threshold:
                    last_sig = sig
                    timestamp = frame_idx / native_fps
                    yield timestamp, gray
            frame_idx += 1
    finally:
        cap.release()


# ---------------------------------------------------------------------------
# OCR
# ---------------------------------------------------------------------------
def ocr_frame(gray) -> str:
    """Run OCR on a grayscale frame and return the raw text."""
    # Light upscaling + thresholding tends to help OCR on phone screenshots.
    scaled = cv2.resize(gray, None, fx=1.5, fy=1.5, interpolation=cv2.INTER_CUBIC)
    _, thresh = cv2.threshold(scaled, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return pytesseract.image_to_string(thresh)


# ---------------------------------------------------------------------------
# Turning OCR text into messages
# ---------------------------------------------------------------------------
def clean_line(line: str) -> str:
    return re.sub(r"[ \t]+", " ", line).strip()


def extract_messages_from_text(text: str, timestamp: float) -> list[dict]:
    """Split one frame's OCR text into candidate messages.

    Each returned dict:
        { "date": <str|None>, "time": <str|None>,
          "message": <str>, "frame_time_sec": <float> }

    Strategy: walk the lines top to bottom. When we see a date-only /
    time-only line, remember it as the 'current' date/time context and
    attach it to the message lines that follow.
    """
    messages: list[dict] = []
    current_date: str | None = None

    for raw in text.splitlines():
        line = clean_line(raw)
        if not line:
            continue

        dates = find_dates(line)
        time_str = find_time(line)

        if looks_like_only_date_or_time(line):
            # It's a separator; update context, don't emit a message.
            if dates:
                current_date = dates[0]
            continue

        # It's a real message line. It may still carry an inline timestamp.
        # Strip a leading/trailing timestamp off the visible message text.
        message_text = line
        if time_str:
            message_text = message_text.replace(time_str, "").strip(" -•·|")

        # Ignore junk lines that are too short to be a real message.
        if len(re.sub(r"[^A-Za-z0-9]", "", message_text)) < 2:
            continue

        messages.append(
            {
                "date": (dates[0] if dates else current_date),
                "time": time_str,
                "message": message_text,
                "frame_time_sec": round(timestamp, 2),
            }
        )
    return messages


def _dedupe_key(msg: dict) -> str:
    """Normalize a message so we can spot repeats across frames."""
    return re.sub(r"[^a-z0-9]", "", msg["message"].lower())


def dedupe_messages(messages: list[dict]) -> list[dict]:
    """Keep the first occurrence of each distinct message, preserving order.

    Because the chat scrolls, the same message shows up in many frames.
    We collapse them, keeping the earliest frame it appeared in and the
    best date/time context we saw for it.
    """
    seen: dict[str, dict] = {}
    ordered: list[str] = []
    for msg in messages:
        key = _dedupe_key(msg)
        if not key:
            continue
        if key not in seen:
            seen[key] = dict(msg)
            ordered.append(key)
        else:
            # Fill in a missing date/time if a later frame had one.
            existing = seen[key]
            if not existing.get("date") and msg.get("date"):
                existing["date"] = msg["date"]
            if not existing.get("time") and msg.get("time"):
                existing["time"] = msg["time"]
    return [seen[k] for k in ordered]


# ---------------------------------------------------------------------------
# Output writers
# ---------------------------------------------------------------------------
def write_outputs(messages: list[dict], out_base: Path, source_name: str) -> None:
    out_base.parent.mkdir(parents=True, exist_ok=True)

    # --- JSON ---
    payload = {
        "source_file": source_name,
        "generated_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "message_count": len(messages),
        "messages": messages,
    }
    out_base.with_suffix(".json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    # --- TXT (human readable) ---
    lines = [
        f"Text messages extracted from: {source_name}",
        f"Generated: {payload['generated_at']}",
        f"Total messages: {len(messages)}",
        "=" * 60,
        "",
    ]
    for i, m in enumerate(messages, 1):
        stamp = " ".join(x for x in (m.get("date"), m.get("time")) if x)
        header = f"[{i:>3}]" + (f"  {stamp}" if stamp else "")
        lines.append(header)
        lines.append(f"      {m['message']}")
        lines.append("")
    out_base.with_suffix(".txt").write_text("\n".join(lines), encoding="utf-8")

    # --- CSV (spreadsheet) ---
    with out_base.with_suffix(".csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["#", "date", "time", "message", "frame_time_sec"])
        for i, m in enumerate(messages, 1):
            writer.writerow(
                [i, m.get("date") or "", m.get("time") or "", m["message"], m.get("frame_time_sec")]
            )


# ---------------------------------------------------------------------------
# Per-file driver
# ---------------------------------------------------------------------------
def process_video(
    video_path: Path,
    outdir: Path,
    target_fps: float,
    change_threshold: int,
    verbose: bool = True,
) -> int:
    """Process one MP4. Returns the number of messages found."""
    if verbose:
        print(f"\n>> Processing: {video_path.name}")

    all_messages: list[dict] = []
    frame_count = 0
    for timestamp, gray in iter_unique_frames(video_path, target_fps, change_threshold):
        frame_count += 1
        text = ocr_frame(gray)
        all_messages.extend(extract_messages_from_text(text, timestamp))
        if verbose and frame_count % 10 == 0:
            print(f"   ...scanned {frame_count} unique frames, "
                  f"{len(all_messages)} raw lines so far")

    messages = dedupe_messages(all_messages)

    out_base = outdir / video_path.stem
    write_outputs(messages, out_base, video_path.name)

    if verbose:
        print(f"   Done: {frame_count} unique frames -> "
              f"{len(messages)} distinct messages")
        print(f"   Wrote: {out_base.with_suffix('.txt')}")
        print(f"          {out_base.with_suffix('.json')}")
        print(f"          {out_base.with_suffix('.csv')}")
    return len(messages)


def collect_videos(target: Path) -> list[Path]:
    if target.is_dir():
        vids = sorted(
            p for p in target.iterdir()
            if p.suffix.lower() in {".mp4", ".mov", ".m4v", ".mkv", ".avi"}
        )
        return vids
    return [target]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def check_dependencies(tesseract_cmd: str | None) -> None:
    problems = []
    if cv2 is None:
        problems.append(
            "  - OpenCV is missing.  Fix:  pip install opencv-python"
        )
    if pytesseract is None:
        problems.append(
            "  - pytesseract is missing.  Fix:  pip install pytesseract"
        )
    if problems:
        print("Missing required Python packages:\n" + "\n".join(problems))
        sys.exit(1)

    if tesseract_cmd:
        pytesseract.pytesseract.tesseract_cmd = tesseract_cmd

    # Verify the Tesseract *engine* (not just the wrapper) is reachable.
    try:
        pytesseract.get_tesseract_version()
    except Exception:
        print(
            "Could not find the Tesseract OCR engine.\n"
            "  Install it, then re-run:\n"
            "    Windows : https://github.com/UB-Mannheim/tesseract/wiki\n"
            "    macOS   : brew install tesseract\n"
            "    Linux   : sudo apt-get install tesseract-ocr\n"
            "  If it's installed but not on PATH, pass its full path with\n"
            '    --tesseract "C:\\Program Files\\Tesseract-OCR\\tesseract.exe"'
        )
        sys.exit(1)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Extract dated text messages from an MP4 screen recording.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("video", help="Path to an MP4 file OR a folder of videos.")
    parser.add_argument(
        "--outdir", default="extracted_messages",
        help="Where to write results (default: extracted_messages).",
    )
    parser.add_argument(
        "--fps", type=float, default=2.0,
        help="Frames per second to inspect (default: 2). Lower = faster.",
    )
    parser.add_argument(
        "--change-threshold", type=int, default=8,
        help="How different a frame must be to be re-OCR'd (default: 8, "
             "range ~0-256). Higher = fewer frames scanned.",
    )
    parser.add_argument(
        "--tesseract", default=None,
        help="Full path to the tesseract executable, if it's not on PATH.",
    )
    args = parser.parse_args(argv)

    check_dependencies(args.tesseract)

    target = Path(args.video)
    if not target.exists():
        print(f"Not found: {target}")
        return 1

    videos = collect_videos(target)
    if not videos:
        print(f"No video files found in: {target}")
        return 1

    outdir = Path(args.outdir)
    total = 0
    for vid in videos:
        try:
            total += process_video(vid, outdir, args.fps, args.change_threshold)
        except Exception as exc:  # keep going through a folder even if one fails
            print(f"   !! Failed on {vid.name}: {exc}")

    print(f"\nAll done. {total} messages extracted across {len(videos)} "
          f"file(s). Results in: {outdir.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
