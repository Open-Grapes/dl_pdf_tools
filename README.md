# dl_pdf_tools

A small PDF tool for Windows and macOS. **View** PDFs, **organize** pages, **split** them, **extract pages** and
**reduce file size**. Your original files are never changed: every tool writes a new file.

**Download:** see [Releases](../../releases). On Windows, get `dl_pdf_tools-windows.zip`, unzip it and run
`dl_pdf_tools.exe`. On a Mac, get the `.dmg` for your Mac (*apple-silicon* for M1 and newer, *intel* for older
Macs) and follow `READ ME.txt` inside it.

## Using it

- **Open:** use `Ctrl+O`, drag a PDF onto the window, or right-click a PDF → *Open with* → `dl_pdf_tools.exe`.
- **Organize (Ctrl+G):** every page as a grid. Drag pages to reorder them; select pages and rotate (L / R), move
  (Ctrl+← / Ctrl+→), delete (Del) or extract them; Undo (Ctrl+Z) and Reset. **Save as new PDF** writes the result.
  Split, Extract and Reduce size use your organized pages while Organize is open.
- **Pages panel (F4):** thumbnails. Click one to jump to that page. Use Ctrl/Shift + click to select several pages, then right-click → *Extract selected pages* or *Split after page N*.
- **Split (Ctrl+Shift+S):** every N pages, one file per page, or custom ranges (`1-3, 4-10, 11-`; each range becomes one file).
- **Extract pages (Ctrl+E):** pick pages such as `1-3, 5, 8-`. When you've selected several pages, it asks whether
  to **combine them into one PDF** or **save each page as its own PDF**.
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
- **Theme:** the Theme menu (top right) switches between *Match system*, *Light* and *Dark*.

On a Mac, use ⌘ where this says Ctrl.

The app remembers your settings between sessions: theme, window size and position, the Pages panel, zoom,
your last Reduce size choice and whether Extract combines pages or saves them separately.

Size estimates are exact for small PDFs (up to 8 pages). For bigger ones the tool compresses a sample of pages and
scales the result up, which is usually within a few percent.

## Updates

Click **Check for updates** (top right). The window shows your version, the latest version on GitHub with its
release date, and that release's *What's new* notes:

- If you already have it, it says *You're on the latest version* ✔ and *Everything is up to date*.
- If there's a newer one, it says *Version X is available*. **Update now** downloads it (with a progress bar),
  closes the app, swaps in the new version and opens it again. Your settings are kept.
- **Check again** asks GitHub again. **Release page** opens the release on GitHub.

It also checks quietly once a day and shows *Update available* in the status bar. You can switch this off in the
Check for updates window.

Updates come from this repository's [Releases](../../releases), so other people only get them while the repository
is **public**. While it's private, the check only works on a computer that is logged in to GitHub with access to
it (through the `gh` tool or a `GH_TOKEN` variable); everyone else sees "No published version was found".

## Web version (in your browser)

Double-click **`dl_pdf_tools (Web).cmd`** in the app folder (on a Mac: **`dl_pdf_tools (Web).command`** in the
.dmg; right-click → Open the first time), or run `dl_pdf_tools.exe --web`. Your browser opens a
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
On a Mac, allow dl_pdf_tools to find devices on your local network when macOS asks.

## Run from source

```
py -3.13 -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python run.py [file.pdf]
```

## Publishing a new version

1. Raise `APP_VERSION` in `dlpt/common.py` (e.g. `1.5.0` → `1.6.0`).
2. Build Windows with `build.py`; push to `main` so GitHub Actions builds the two Mac `.dmg` files.
3. Create a GitHub release tagged `v1.6.0` with `dl_pdf_tools-windows.zip`,
   `dl_pdf_tools-mac-apple-silicon.dmg` and `dl_pdf_tools-mac-intel.dmg` attached (keep these exact names: the
   updater looks for them). The release notes are what users see under "What's new".

## Build the .exe

```
.venv\Scripts\python build.py
```

Output: `dist\Windows\dl_pdf_tools\dl_pdf_tools.exe`, plus a zip of that folder for sharing.

## Build for macOS

GitHub Actions builds the Mac app on every push to `main` (`.github/workflows/mac.yml`): one `.dmg` for Apple
Silicon and one for Intel, each smoke-tested on a real Mac. Download them from the workflow run's *Artifacts*.
On a Mac you can also run `python3 build_mac.py` (output in `dist/MacOS/`).

The Mac app is signed ad hoc but not notarized by Apple, so the first launch needs *Open Anyway* in
System Settings → Privacy & Security.

## Licences

dl_pdf_tools is licensed under the GNU AGPL-3.0 (see `LICENSE`).

Built with [PyMuPDF](https://pymupdf.readthedocs.io) (AGPL-3.0) and Qt for Python / PySide6 (LGPL-3.0).
You can use it freely. If you distribute the app, the AGPL requires you to offer its source code too.
