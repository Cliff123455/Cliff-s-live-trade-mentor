# MP4 Text-Message Extractor

Give this to another Claude/coder along with the MP4. It turns a **screen
recording of a text conversation** into a clean, dated list of messages.

## What it does
1. Samples frames from the MP4 (a couple per second is plenty).
2. Skips frames that didn't change (scrolling chats repeat themselves).
3. OCRs each unique frame.
4. Pulls out message lines + any dates/timestamps it can find.
5. De-duplicates messages across the whole video.
6. Writes a `.txt`, `.json`, and `.csv` per video.

## Setup (one time)
```bash
pip install opencv-python pytesseract
```
Plus the Tesseract OCR **engine** itself:
- **Windows:** installer at https://github.com/UB-Mannheim/tesseract/wiki
- **macOS:** `brew install tesseract`
- **Linux:** `sudo apt-get install tesseract-ocr`

## Run it
```bash
# one file
python tools/mp4_text_extractor.py chat.mp4

# a whole folder of videos, custom output dir
python tools/mp4_text_extractor.py ./recordings --outdir results

# if Tesseract isn't on your PATH (common on Windows)
python tools/mp4_text_extractor.py chat.mp4 --tesseract "C:\Program Files\Tesseract-OCR\tesseract.exe"
```

## Output (per video `chat.mp4`)
- `chat.txt` — readable transcript, one dated message per block
- `chat.json` — structured data (message, date, time, frame time)
- `chat.csv` — open in Excel/Sheets

## Handy flags
| Flag | Default | What it does |
|------|---------|--------------|
| `--fps` | `2` | Frames per second to inspect. Lower = faster. |
| `--change-threshold` | `8` | How different a frame must be to re-OCR it. Higher = fewer frames scanned. |
| `--outdir` | `extracted_messages` | Where results go. |
| `--tesseract` | (PATH) | Full path to the tesseract executable. |

## Notes
- OCR is never 100%. You get every line it found, so you can eyeball and
  fix afterward — the CSV is the easiest place to clean up.
- Dates shown as separators (e.g. `Today`, `January 5, 2024`) are carried
  forward and attached to the messages beneath them.
