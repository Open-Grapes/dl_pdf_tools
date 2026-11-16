# dl_pdf_tools

A small Windows PDF tool. **View** PDFs, **split** them, **extract pages** and **reduce file size**.
No editing; your original files are never changed. Every tool writes a new file.

## Using it

- **Open:** use `Ctrl+O`, drag a PDF onto the window, or right-click a PDF → *Open with* → `dl_pdf_tools.exe`.
- **Pages panel (F4):** thumbnails. Click one to jump to that page. Use Ctrl/Shift + click to select several pages, then right-click → *Extract selected pages* or *Split after page N*.
- **Split (Ctrl+Shift+S):** every N pages, one file per page, or custom ranges (`1-3, 4-10, 11-`; each range becomes one file).
- **Extract pages (Ctrl+E):** pick pages such as `1-3, 5, 8-` and save them into one new PDF, or one PDF per page.
- **Reduce size (Ctrl+R):**
  - **Light:** images capped at 220 dpi, JPEG quality 85. You won't notice a difference.
  - **Balanced:** 150 dpi, quality 72. Good for sharing by email.
  - **Strong:** 96 dpi, quality 55. Smallest file. Fine on screen, rough when printed.
  - **Custom:** set your own dpi and quality.
  - Options: convert images to grayscale, remove document properties, or lossless only (images untouched).

  Text and vector graphics stay sharp. The tool also trims embedded fonts and removes unused data.
  If the PDF can't be made smaller, it tells you and writes no file.
- **Zoom:** Ctrl + mouse wheel, `Ctrl +` / `Ctrl −`, `Ctrl+0` fit width, `Ctrl+9` fit page.

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
