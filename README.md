# dl_pdf_tools

A small Windows PDF tool. **View** PDFs, **split** them, **extract pages** and **reduce file size**.
No editing; your original files are never changed. Every tool writes a new file.

## Using it

- **Open:** use `Ctrl+O`, drag a PDF onto the window, or right-click a PDF → *Open with* → `dl_pdf_tools.exe`.
- **Pages panel (F4):** thumbnails. Click one to jump to that page. Use Ctrl/Shift + click to select several pages, then right-click → *Extract selected pages* or *Split after page N*.
- **Split (Ctrl+Shift+S):** every N pages, one file per page, or custom ranges (`1-3, 4-10, 11-`; each range becomes one file).
- **Extract pages (Ctrl+E):** pick pages such as `1-3, 5, 8-` and save them into one new PDF, or one PDF per page.
  Tick **Also reduce file size** to choose a Reduce size setting for the extracted pages, with a size estimate.
- **Reduce size (Ctrl+R):** every choice shows its **estimated file size** before you compress.
  - **Light:** images capped at 220 dpi, JPEG quality 85. You won't notice a difference.
  - **Balanced:** 150 dpi, quality 72. Good for sharing by email.
  - **Strong:** 96 dpi, quality 55. Smallest file. Fine on screen, rough when printed.
  - **Custom:** set your own dpi and quality.
  - Options: convert images to grayscale, remove document properties, or lossless only (images untouched).

  Text and vector graphics stay sharp. The tool also trims embedded fonts and removes unused data.
  If the PDF can't be made smaller, it tells you and writes no file.
- **Zoom:** Ctrl + mouse wheel, `Ctrl +` / `Ctrl −`, `Ctrl+0` fit width, `Ctrl+9` fit page.

Size estimates are exact for small PDFs (up to 8 pages). For bigger ones the tool compresses a sample of pages and
scales the result up, which is usually within a few percent.

## Web version (in your browser)

Double-click **`dl_pdf_tools (Web).cmd`** in the app folder, or run `dl_pdf_tools.exe --web`. Your browser opens a
page with the same viewer and tools. Results arrive as downloads; several files come as one .zip.

It all runs on your own computer. The page talks to a small server that only this PC can reach (127.0.0.1),
protected by a random key for each session. Your PDFs never go to the internet. The server stops by itself a
minute after you close the page.

## Use it from your phone (Share via Wi-Fi)

Click **Share via Wi-Fi** in the toolbar. A window shows a QR code and two addresses, such as
`http://192.168.0.106` and `http://dl-pdf-tools.local`. On your phone (on the same Wi-Fi), point the camera at the
code or type an address into Safari or Chrome. You get the same viewer and tools, laid out for a phone:

- **Open PDF** picks a file from the phone (Files, iCloud Drive, …).
- **Pages** shows every page as a grid. Tap a page to jump to it, or tap **Select** and then pages to pick them for
  Extract pages.
- Results download to the phone. On iPhone, tap **Download** and find the file in the Files app → Downloads.

The PC does the work, so it must be on with dl_pdf_tools open. Sharing stays on until you click **Turn off** or close
the app. There's no password, so anyone on the same Wi-Fi who knows the address can open the page while sharing is on.

**Phone can't connect?** The first time, Windows asks whether to allow dl_pdf_tools on networks: allow it on
*Private networks*. Your Wi-Fi must also be set to *Private* in Windows Settings → Network & internet.

## Run from source

```
py -3.13 -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python run.py [file.pdf]
```

## Build the .exe

```
.venv\Scripts\python build.py
```

Output: `dist\Windows\dl_pdf_tools\dl_pdf_tools.exe`, plus a zip of that folder for sharing.

## Licences

dl_pdf_tools is licensed under the GNU AGPL-3.0 (see `LICENSE`).

Built with [PyMuPDF](https://pymupdf.readthedocs.io) (AGPL-3.0) and Qt for Python / PySide6 (LGPL-3.0).
You can use it freely. If you distribute the app, the AGPL requires you to offer its source code too.
