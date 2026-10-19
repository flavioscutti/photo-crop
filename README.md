<p align="center">
  <img src="https://github.com/user-attachments/assets/634b0c79-13a7-4a66-9ac8-24161e8194a0" alt="Photo Crop logo" width="200">
</p>

# Photo Crop

**Batch crop and straighten of analog photo scans, on Raspberry Pi.**

Photo Crop takes the scans of your printed photographs, finds the print on the scanner bed, straightens it, crops it to the paper edge and saves it in the format you want, with the original DPI, ICC profile and metadata preserved. It works in fully automatic mode, in a precision manual mode with zoomed corner and edge inspectors, or in a mix of the two.

Written in Python with Tkinter, OpenCV, Pillow and exiftool. Dark, touchscreen-friendly interface.

<br>

<p align="center">
  <img src="https://github.com/user-attachments/assets/86a9a985-14d4-466f-8828-aff07414cf7b" alt="Photo Crop main window" width="700">
</p>

<br>

## Table of contents

- [Why this tool?](#why-this-tool)
- [Features at a glance](#features-at-a-glance)
- [System requirements](#system-requirements)
- [Files in order of operation](#files-in-order-of-operation)
- [Quick installation](#quick-installation)
- [Usage](#usage)
  - [Quick start](#quick-start)
  - [Main window](#main-window)
  - [Selecting scans](#selecting-scans)
  - [Choosing where files are saved](#choosing-where-files-are-saved)
  - [Auto and Manual crop modes](#auto-and-manual-crop-modes)
  - [The manual inspector window](#the-manual-inspector-window)
- [Output Formats window](#output-formats-window)
  - [General tab](#general-tab)
  - [Metadata tab](#metadata-tab)
  - [JPEG tab](#jpeg-tab)
  - [TIFF tab](#tiff-tab)
  - [PNG tab](#png-tab)
  - [The DPI panel](#the-dpi-panel)
  - [Settings reference](#settings-reference)
- [How files are saved](#how-files-are-saved)
- [Metadata handling](#metadata-handling)
- [How the detection works](#how-the-detection-works)
- [Performance and memory](#performance-and-memory)
- [Unusual file names](#unusual-file-names)
- [Log messages](#log-messages)
- [Keyboard shortcuts](#keyboard-shortcuts)
- [Configuration file](#configuration-file)
- [Example workflows](#example-workflows)
- [Compatibility](#compatibility)
- [Notes and troubleshooting](#notes-and-troubleshooting)
- [Built with](#built-with)
- [Author](#author)
- [License](#license)

## Why this tool?

Scanning a shoebox of prints usually means scanning one photo at a time, then fixing each file by hand: straighten a slightly crooked print, trim the white scanner background, copy the resolution and colour profile, save a master and a web copy. Photo Crop does all of that in one pass:

- it **detects the paper edge** with sub-pixel precision and straightens the print by the measured angle;
- it **crops to the largest rectangle that fits inside the print**, so no scanner background or paper border is left on the edges;
- it **keeps the characteristics of the scan**: bit depth (8 or 16 bit), DPI, ICC profile, EXIF, XMP and IPTC;
- it can **save several formats from a single confirmation** (for example a 16-bit TIFF master plus a JPEG copy);
- it lets you **review every photo with a precision inspector** whenever you want full control, and skips the window entirely when you trust the detection.

## Features at a glance

**Input**
- Reads JPEG, PNG, TIFF and BMP scans (`.jpg`, `.jpeg`, `.png`, `.tif`, `.tiff`, `.bmp`, in any letter case).
- Native 8-bit and 16-bit support: the scan is read once at its own bit depth.
- Applies the EXIF/TIFF orientation tag itself, so results do not depend on the OpenCV version.
- Any file name works: spaces, symbols, parentheses, unusual glyphs and even undecodable bytes.
- Multiple selections accumulate; duplicates are skipped.

**Detection and straightening**
- Automatic detection of the four corners of the print (Sobel gradients, Hough lines, sub-pixel line fitting, line intersection).
- Automatic fallback method when the main one cannot find four reliable edges.
- Automatic straightening angle, computed from the horizontal edges or from the vertical edges, whichever needs the smaller correction.
- Crop rectangle = largest axis-aligned rectangle inscribed in the rotated print.
- Three interpolation methods for rotation: Linear, Bicubic, Lanczos. At exactly 0 degrees pixels are never resampled.

**Crop modes**
- **Auto**: no window, the whole batch runs unattended. Photos whose detection looks doubtful are automatically queued for a manual review at the end.
- **Manual**: a precision inspector for each photo with a full preview, a corner zoom and an edge zoom, mouse and keyboard control, angle fine-tuning, 90-degree rotation, and one-key corner selection.

**Output**
- JPEG, TIFF and PNG, alone or together, or "Same as original" (each file keeps its own format).
- JPEG: quality, chroma subsampling (4:4:4, 4:2:2, 4:2:0), progressive, Huffman optimisation.
- TIFF: None, PackBits, LZW or Deflate compression.
- PNG: compression level 1 to 9.
- 8/16-bit handling for TIFF and PNG (keep original or convert to 8-bit).
- "As original" mode per format: reproduces the quality, subsampling, progressive flag or TIFF compression of the source file.
- DPI: keep the original, or force any value from 1 to 100000 (px/in or px/cm), with presets up to 2400.
- Metadata: copy EXIF, GPS, XMP, IPTC and ICC profile from the original scan; optionally write your own values (camera, ISO, film, paper, date, author, copyright, scanner software, GPS).
- Output naming: `_cropped` suffix or overwrite the original; existing files are never overwritten by accident (`_1`, `_2`, ...).
- Custom output folder, with a built-in folder browser that can create new folders.

**Speed and safety**
- The next scan is loaded and analysed in the background while you work on the current one.
- Cropping and writing run in a background thread, with a cap on images waiting for the disk, to keep RAM usage low on the Raspberry Pi.
- Files are written to a temporary hidden file and moved into place only when complete.
- All settings are remembered between sessions and validated on load.

## System requirements

| Requirement | Details |
|---|---|
| Hardware | Raspberry Pi (developed and tested on a Raspberry Pi 4 with 8 GB of RAM) or any Linux PC |
| Operating system | Raspberry Pi OS, or any Debian-based Linux distribution |
| Python | Python 3 with Tkinter |
| Python libraries | OpenCV 4.5.4 or newer (the headless build is enough: the app never uses OpenCV's own GUI, the whole interface is Tkinter), NumPy 1.21 or newer, Pillow 9.1 or newer (with ImageTk) |
| External tool | exiftool (strongly recommended: needed to copy DPI, ICC profile, EXIF, XMP and to write manual metadata) |
| Font | Inter (package `fonts-inter`), used by the whole interface |
| Display | Works with mouse and keyboard or on a touchscreen |

## Files in order of operation

| Order | File | What it does |
|---|---|---|
| 1 | `install.sh` | Installation script for Raspberry Pi OS |
| 2 | `requirements.txt` | Python dependencies and their minimum versions |
| 3 | `photo_crop.py` | The application |
| 4 | `.assets/logo.png` | Logo shown in the About window |
| 5 | `README.md` | This file |
| 6 | `LICENSE` | GNU GPL v3.0 |

A ready-to-install **`.deb` package** is published on the [Releases page](https://github.com/flavioscutti/photo-crop/releases/tag/v1.6.7).

The program creates one file of its own in your home folder: `~/.photo_crop_config.json` (see [Configuration file](#configuration-file)).

## Quick installation

### Option A: install from the APT repository (recommended)

This is the cleanest way to install Photo Crop on Debian, Ubuntu or Raspberry Pi OS. It allows you to receive updates automatically with `sudo apt upgrade`.

Copy and paste the following three commands in a terminal:

# 1. Add the GPG key
```bash
curl -fsSL https://flavioscutti.github.io/photo-crop/public.key | sudo gpg --dearmor -o /usr/share/keyrings/photo-crop.gpg
```

# 2. Add the repository
```bash
echo "deb [signed-by=/usr/share/keyrings/photo-crop.gpg] https://flavioscutti.github.io/photo-crop stable main" | sudo tee /etc/apt/sources.list.d/photo-crop.list
```
# 3. Install Photo Crop
```****
sudo apt update
sudo apt install photo-crop
```
Photo Crop is now installed and appears in the Graphics menu. To update it in the future, simply run:

```bash
sudo apt update && sudo apt upgrade
```

### Option B: install the .deb package

Download the `.deb` file from the [v1.6.7 release](https://github.com/flavioscutti/photo-crop/releases/tag/v1.6.7), then install it with `apt` so that the dependencies are resolved automatically:

```bash
cd ~/Downloads
sudo apt install ./photo-crop_*.deb
```

Photo Crop then appears in the **Graphics** menu. On some systems the menu refreshes only after logging out and back in, or after a reboot.
```

### Option C: run from the source code

```bash
git clone https://github.com/flavioscutti/photo-crop.git
cd photo-crop
chmod +x install.sh
./install.sh
python3 photo_crop.py
```

To update later:

```bash
cd ~/photo-crop
git pull
```

### Option C: create the files by hand

Install the dependencies:

```bash
sudo apt update
sudo apt install python3-tk python3-opencv python3-numpy python3-pil python3-pil.imagetk libimage-exiftool-perl fonts-inter
```

Create the folder and the script with a text editor such as Mousepad, paste the code into it and save:

```bash
mkdir -p ~/photo-crop
mousepad ~/photo-crop/photo_crop.py &
```

Run it:

```bash
python3 ~/photo-crop/photo_crop.py
```

### Other Linux distributions

Install the same components with your package manager.

| Distribution | Command |
|---|---|
| Debian, Ubuntu, Raspberry Pi OS | `sudo apt install python3-tk python3-opencv python3-numpy python3-pil python3-pil.imagetk libimage-exiftool-perl fonts-inter` |
| Fedora | `sudo dnf install python3-tkinter python3-opencv python3-numpy python3-pillow python3-pillow-tk perl-Image-ExifTool rsms-inter-fonts` |
| Arch Linux | `sudo pacman -S tk python-opencv python-numpy python-pillow perl-image-exiftool inter-font` |

If your distribution does not provide a package, `pip install "opencv-python-headless>=4.5.4" "numpy>=1.21" "Pillow>=9.1.0"` also works. Without the Inter font Tkinter falls back to a default font and the layout may look slightly different.

## Usage

### Quick start

1. Start **Photo Crop**.
2. Press **1. Select Scan Images**, browse to your scans, select one or more files and press **Confirm Selection**.
3. (Optional) Choose `Add '_cropped' suffix` or `Overwrite Original`, pick the output folder with **Save to**, and open **Save as** to choose formats, compression, DPI and metadata.
4. Choose **Auto** (unattended) or **Manual** (precision review of every photo).
5. Press **2. Start Cropping**.
6. In Manual mode, check the crop rectangle for each photo and press **Confirm & Crop** (or `Enter`). In Auto mode just wait: the log shows each file as it is processed.
7. When the batch ends, a message shows how many files were saved.

### Main window

The main window (1024 x 834 by default, resizable down to 800 x 680) contains, from top to bottom:

| Element | Description |
|---|---|
| **PHOTO CROP** | Title |
| **1. Select Scan Images** | Opens the file selector. After the first selection the label becomes **1. Add More Scans**, because selections accumulate |
| Status line | Shows `No files selected`, then `N scan images ready`, then `Processing [i/N]: file name` during the batch, and `Completed! x/N scans processed.` at the end |
| **Add '_cropped' suffix** / **Overwrite Original** | Choose how output files are named (see [How files are saved](#how-files-are-saved)). Suffix is the default at every start |
| **Save to:** | Choose the output folder. Default: `Same folder as source files`. Long paths are shortened on the button. The choice is remembered |
| **Save as:** | Shows a summary of the selected output formats (for example `Original`, `JPEG 95%`, `TIFF LZW + JPEG 95%`) and opens the [Output Formats window](#output-formats-window) |
| **Auto** / **Manual** | Crop mode. The choice is remembered |
| **2. Start Cropping** | Starts the batch. It is greyed out until at least one scan is in the list. Pressing `Enter` in the main window does the same |
| Log | A scrolling log with an auto-hiding scrollbar. It keeps the complete history of the session and is cleared only when you close the app |

Other behaviour of the main window:

- **Selections add up.** Each time you select files they are appended to the list. Files already in the list are recognised by their full path and skipped, with a note in the log.
- **After a batch** the list is emptied, the buttons return to their normal state, and the log is kept. A confirmation message appears if at least one file was saved.
- **During a batch** the Select, Save to, Save as and Start buttons are disabled.
- **Before starting**, if you chose an output folder, the program checks that it still exists and is writable, and warns you if not.
- **Missing exiftool:** if you asked for metadata, DPI or ICC handling and exiftool is not installed, a warning with the installation command is printed in the log. Images are still cropped and saved.

### Selecting scans

The file selector (**Select Image Scans**, 1024 x 768, modal) is a touch-friendly browser that only shows folders and supported image files:

- Rows are labelled `[DIR]` for folders and `[IMG]` for images. Folders come first, then files, each sorted alphabetically without regard to case.
- **Multiple selection** is supported (click, Shift-click, Ctrl-click, or drag).
- Double-click or `Enter` on a folder opens it; on an image (or on a multiple selection) it confirms the selection.
- `Right Arrow` enters the selected folder; `Left Arrow` or the **Parent Folder** button goes up one level.
- **Confirm Selection** accepts the selected files; **Cancel (Esc)** closes the window. `Ctrl+W` and `Ctrl+Q` also cancel.
- The last folder you browsed is remembered and used the next time.
- If you press Confirm with nothing selected, you get a warning.

### Choosing where files are saved

Press **Save to** to open the folder selector (**Select Output Folder**):

- Only folders are listed (hidden folders are not shown).
- **Use This Folder** selects the folder currently displayed; it refuses folders that are not writable.
- **Same as Source** goes back to the default: each output is saved next to its original.
- **New Folder** asks for a name, creates the folder in the current location and selects it.
- **Parent Folder (Left Arrow)**, double-click, `Enter` and `Right Arrow` navigate as in the file selector. **Cancel (Esc)** leaves the setting unchanged.

The chosen folder is remembered between sessions and written in the log.

### Auto and Manual crop modes

| | Auto | Manual |
|---|---|---|
| Window per photo | No | Yes, the precision inspector |
| Who decides the crop | The detection | You, starting from the detection |
| Doubtful detections | Queued for manual review at the end of the batch | Always reviewed |
| Best for | Clean scans with a clear paper edge | Difficult prints, archival work, final quality checks |

The mode is saved and restored at the next start. The two modes can be mixed in the same batch: in Auto mode, any photo the program does not trust is reviewed by you at the end, and this review does not change your Auto/Manual setting.

#### What Auto mode does

For each photo the program detects the print, computes the straightening angle, builds the inscribed crop rectangle and saves the result exactly as the manual window would, without showing anything.

A detection is **not trusted** and the photo is sent to the manual review queue when any of these is true:

- the main detection method failed and the fallback method was used;
- the detected print covers less than 5% of the image (probably a false detection);
- the detected quadrilateral touches all four borders of the scan (there is no real margin to crop);
- the rotation angle is greater than 45 degrees;
- the resulting crop area is empty.

The log reports each case, for example `[AUTO->MANUAL] photo.tif: detected area is only 3.2% of the image`. When the batch reaches the end, the inspector opens for just those photos (`Manual review for N photo(s) skipped by auto-detection...`).

### The manual inspector window

In Manual mode (and for the Auto review queue) the **Interactive Inspector** opens, titled `PRECISION CROP REVIEW: file name`. The window is created once and reused for the whole batch; its size and position are remembered. By default it fills the screen up to 1900 x 1010 pixels.

<p align="center">
  <img src="https://github.com/user-attachments/assets/2df5114e-ddf9-43b4-b1c9-9e8a374ad8f2" alt="Interactive Inspector window" width="800">
</p>

**Layout**

- **Left half:** the whole rotated photo with the crop rectangle drawn in cyan. The four corner handles are shown as dots; the selected corner is larger and white.
- **Right half, top:** four corner tabs, **1 Top-Left**, **2 Top-Right**, **3 Bottom-Right**, **4 Bottom-Left**, then the angle and zoom controls.
- **Right half, middle:** two magnified views side by side:
  - the **corner zoom** shows the real pixels around the selected corner with a red dashed crosshair, and prints its `X, Y` coordinates under the picture;
  - the **edge zoom** shows the middle of the side that is being moved, with a cyan crosshair, and is labelled `Top Side`, `Bottom Side`, `Left Side` or `Right Side`.
- **Right half, bottom:** an on-screen arrow pad (UP, LEFT, DOWN, RIGHT) and a **Step** button.
- **Bottom bar:** **Reset Auto Rect**, **Horizontal**, **Vertical**, **Confirm & Crop**, **Skip Image**.

The magnified views use nearest-neighbour scaling, so you see exact pixels without smoothing.

**Controls**

| Control | What it does |
|---|---|
| **Angle  -  value  +** | Fine-tunes the straightening angle. Positive values rotate clockwise. The label shows the total angle |
| **Angle step button** | Cycles the angle step between `0.01`, `0.10` and `1.00` degrees |
| **90 CCW** / **90 CW** | Rotates the whole photo by 90 degrees (the crop rectangle swaps width and height and stays centred). Use it for scans that are sideways |
| **Zoom `-` / `+`** | Changes the size of the inspected area: 25, 45 (default), 100 or 200 source pixels. A smaller area means more magnification. The label shows the current value |
| **UP / LEFT / DOWN / RIGHT** | Moves the selected corner by one step. The axis you move along decides which edge the edge zoom shows |
| **Step: 1px** | Cycles the nudge step between 1, 10 and 20 pixels |
| **Reset Auto Rect** | Recreates the automatically detected rectangle. If you had switched the alignment, it returns to the alignment with the smaller angle |
| **Horizontal** / **Vertical** | Chooses which pair of edges is used to compute the straightening angle: the top and bottom edges of the print, or the left and right edges. The program preselects the one that needs the smaller correction, and recomputes the angle and the rectangle when you switch |
| **Confirm & Crop** | Crops with the current angle and rectangle and loads the next photo |
| **Skip Image** | Leaves this photo untouched and moves on |

**Moving the crop rectangle**

- **Drag a corner on the full preview.** You can grab a handle within about 30 screen pixels, or the nearest handle within about 60. The rectangle always stays axis-aligned: moving a corner moves the two edges that meet there.
- **Drag inside the corner zoom** for very fine adjustments: the movement is divided by 8.
- **Use the arrow keys or the on-screen pad** to nudge by 1, 10 or 20 pixels.
- **Select several corners at once** by holding several number keys together (for example hold `1` and `2` and press an arrow key to move the whole top edge).
- The rectangle is always kept inside the image.

When you change the angle, the crop rectangle keeps its size and is re-centred; when you press **Reset Auto Rect** or switch **Horizontal / Vertical**, it is recomputed from the detected print.

**Straightening details.** Rotation enlarges the canvas so nothing is cut off, and areas outside the original scan are filled by replicating the nearest edge pixels. The preview is computed from an 8-bit copy of the scan; the final crop of 16-bit scans is computed again from the full-depth original with the same matrix and the same interpolation.

## Output Formats window

Press **Save as:** on the main window to open **Output Formats** (1024 x 834, modal). It has five tabs: **General**, **Metadata**, **JPEG**, **TIFF** and **PNG**, and three buttons:

- **Save** stores the settings (and refuses to close if no output format is selected);
- **Defaults** restores every setting to its factory value in the window (you still press Save to keep it);
- **Cancel (Esc)** closes without changes (`Ctrl+W` also cancels).

All settings are stored in the configuration file and used for every batch until you change them. They are read once at the start of each batch.

### General tab

<p align="center">
  <img src="https://github.com/user-attachments/assets/06419a02-eec5-4d11-bc7b-3051abf51bec" alt="Output Formats window, General tab" width="700">
</p>

**Save these formats together** - four toggles: **Same as original**, **JPEG**, **TIFF**, **PNG**.

- **Same as original** keeps each file's own format: a JPEG scan produces a JPEG, a TIFF scan a TIFF, a PNG scan a PNG, a BMP scan a BMP, and the original extension is preserved.
- **JPEG, TIFF and PNG** can be turned on together: one confirmation then writes every selected format (for example a TIFF master plus a JPEG for sharing).
- "Same as original" and the explicit formats are mutually exclusive: turning one on turns the other off. At least one output always stays selected: if you switch off the last one, the window selects a sensible replacement (Same as original, or JPEG).
- Every format uses the settings of its own tab.

**Straightening (rotation)** - choose the interpolation used when the angle is not 0:

| Method | Notes |
|---|---|
| Linear | Fastest |
| Bicubic | Default, the standard choice |
| Lanczos | Sharpest, slower |

At exactly 0 degrees the pixels are copied without any resampling, whatever you choose here.

**Metadata** - the **Edit metadata** button jumps to the Metadata tab, and a hint tells you whether exiftool was found. If it is missing, the hint shows the install command:

```bash
sudo apt install libimage-exiftool-perl
```

The **(i)** button at the bottom opens the **About** window with logo, name, tagline, version, author and technologies.

### Metadata tab

<p align="center">
  <img src="https://github.com/user-attachments/assets/110d0cce-0ebe-496b-a7dc-26d16193b9d7" alt="Output Formats window, Metadata tab" width="700">
</p>

Write your own metadata into every file of the batch. **Only filled fields are written; empty fields are ignored**, and the values you type override those copied from the original scan. The page scrolls with the mouse wheel or by dragging with a finger.

| Section | Fields |
|---|---|
| Shooting data | Make, Model, ISO, Aperture (f/), Shutter (s), Focal (mm), Film, Paper |
| Date | Date taken, Date digitized (each with a **Today** button) |
| Author / Copyright | Author, Copyright, Software (scanner) |
| GPS (optional) | Latitude (decimal), Longitude (decimal), Altitude (m) |

**Dates** are typed as `DD/MM/YYYY HH:MM:SS` (for example `15/08/2024 14:30:00`) and converted to the standard EXIF format. The separators `/`, `-` and `.` are accepted, the time part is optional (missing parts become `00`), and two-digit years are expanded (70 to 99 become 1970 to 1999, 00 to 69 become 2000 to 2069). A text that does not look like a date is passed to EXIF unchanged, so you can type raw EXIF values. **Today** inserts the current date and time.

**GPS** values are decimal degrees; use negative numbers for South and West (for example `44.4949` and `11.3426`). Altitude is in metres, negative values mean below sea level.

**Film and Paper** are combined into a single text written to the EXIF UserComment and the XMP Description, in the form `Film: X | Paper: Y` (only the parts you filled are written).

The exact tags written are listed in [Metadata handling](#metadata-handling).

### JPEG tab

<p align="center">
  <img src="https://github.com/user-attachments/assets/45b38aaf-b79f-48e4-9bb1-819117eb492a" alt="Output Formats window, JPEG tab" width="700">
</p>

| Setting | Options |
|---|---|
| **As original** | When the source is a JPEG, uses its quality level, chroma subsampling and progressive flag; for any other source the values shown here are used. While on, the compression controls are greyed out (their values stay visible) |
| **Quality** | 60 to 100, with a slider and `-` / `+` buttons for single steps. Default 95 |
| **Chroma subsampling** | `4:4:4 (best)`, `4:2:2`, `4:2:0 (smallest)`. Default 4:2:0. Written through Pillow, so it works with any OpenCV version |
| **Progressive** | Toggle |
| **Optimize Huffman** | Toggle |
| **DPI** | See [The DPI panel](#the-dpi-panel) |
| **Save EXIF + ICC** / **Save XMP** | See [Metadata handling](#metadata-handling) |

When "As original" is on and the source is a JPEG, the quality is estimated from the file's luminance quantisation table, so the re-saved crop matches the quality of the scan without a further quality loss from guessing too low.

JPEG is always 8-bit: 16-bit scans are converted to 8-bit for this format.

### TIFF tab

<p align="center">
  <img src="https://github.com/user-attachments/assets/178084a0-7b65-4e03-b353-41803253cf29" alt="Output Formats window, TIFF tab" width="700">
</p>

| Setting | Options |
|---|---|
| **As original** | When the source is a TIFF, uses its compression method (when it can be read); otherwise the value shown |
| **Method** | A slider from weakest to strongest: `None`, `PackBits`, `LZW` (default), `Deflate`. All TIFF compressions here are lossless |
| **Bit depth** | `Keep original (8 / 16 bit)` or `Convert to 8-bit` |
| **DPI** | See [The DPI panel](#the-dpi-panel) |
| **Save EXIF + ICC** / **Save XMP** | See [Metadata handling](#metadata-handling) |

The compression slider needs an OpenCV build that can set the TIFF compression. If yours cannot, the slider is disabled and the window says so.

### PNG tab

<p align="center">
  <img src="https://github.com/user-attachments/assets/98aae3c5-16ab-4fd5-8886-7f1d645116dd" alt="Output Formats window, PNG tab" width="700">
</p>

| Setting | Options |
|---|---|
| **As original** | PNG files do not record their compression level, so "As original" uses the value shown |
| **Level** | 1 to 9 (1 = fastest, 9 = smallest). PNG is lossless: the level only changes file size and saving time. Default 1 |
| **Bit depth** | `Keep original (8 / 16 bit)` or `Convert to 8-bit` |
| **DPI** | See [The DPI panel](#the-dpi-panel) |
| **Save EXIF + ICC** / **Save XMP** | See [Metadata handling](#metadata-handling) |

### The DPI panel

Each of the three format tabs has the same DPI panel:

- a **DPI** field that shows either the word `Auto` or a number from 1 to 100000 (type it and press `Enter`);
- a **unit** menu: `in` (pixels per inch) or `cm` (pixels per centimetre);
- **presets**: `Auto`, 72, 96, 150, 300, 600, 1200, 2400;
- a **Force DPI** indicator light.

**Auto** keeps the DPI of the original scan. Any other value is written into the file (resolution X, resolution Y and unit), which is useful when you know the real scan resolution and the original file did not record it correctly. The **Force DPI** light is on only when a value will really be written: that is, when "As original" is off and the DPI is not Auto. DPI settings are never applied while "As original" is on.

### Settings reference

| Setting | Default | Range / options |
|---|---|---|
| Same as original | On | On / Off |
| JPEG, TIFF, PNG outputs | Off | Any combination (exclusive with Same as original) |
| Interpolation | Bicubic | Linear, Bicubic, Lanczos |
| Crop mode | Manual | Auto, Manual |
| JPEG: as original | On | On / Off |
| JPEG: quality | 95 | 60 to 100 |
| JPEG: chroma subsampling | 4:2:0 | 4:4:4, 4:2:2, 4:2:0 |
| JPEG: progressive / optimize Huffman | Off / Off | On / Off |
| TIFF: as original | On | On / Off |
| TIFF: compression | LZW | None, PackBits, LZW, Deflate |
| TIFF: bit depth | Keep | Keep, 8-bit |
| PNG: as original | On | On / Off |
| PNG: level | 1 | 1 to 9 |
| PNG: bit depth | Keep | Keep, 8-bit |
| EXIF + ICC / XMP (each format) | On / On | On / Off |
| DPI (each format) | Auto | Auto, or 1 to 100000 in px/in or px/cm |
| Manual metadata fields | Empty | See [Metadata tab](#metadata-tab) |

While "As original" is on for a format, its compression controls and DPI panel are greyed out, and for TIFF and PNG the bit depth is forced to "Keep original", so a 16-bit scan can never be reduced to 8 bits by accident.

## How files are saved

**Naming**

| Mode | Result for `scan.tif` |
|---|---|
| `Add '_cropped' suffix` | `scan_cropped.tif` |
| `Overwrite Original` | `scan.tif`, replacing the source file when the output goes to the same folder and has the same name and extension |

In both modes, if the destination file name already exists (and is not the source being overwritten), the program adds `_1`, `_2`, and so on, so nothing is overwritten by accident. This also applies to files that are still being written in the same batch. When you save several formats, each one gets its own extension: `.jpg`, `.tif`, `.png`; with "Same as original" the original extension (and its letter case) is kept.

> **Warning:** `Overwrite Original` replaces your scan with the cropped version. Work on copies, or keep the suffix mode on, until you are sure of the result.

**Where:** next to the original by default, or in the folder chosen with **Save to**.

**What is saved**

- The crop is computed from the straightened image, and the integer crop box is rounded outwards to whole pixels.
- For **8-bit** scans the crop uses the very pixels shown in the preview.
- For **16-bit** scans, TIFF and PNG with "Keep original" are cropped directly from the full-depth original (only the pixels of the crop are computed), so no tonal information is lost. JPEG and 8-bit conversions are derived from it.
- JPEG and BMP are always 8-bit. A BMP source with "Same as original" produces an 8-bit BMP without metadata copy.
- Images are processed as 3-channel colour images.

**Safe writing**

- Each file is written first to a hidden temporary file (`.photocrop_...`) in the destination folder and moved into place only when complete, so an interrupted run never leaves a half-written image.
- Metadata is copied onto the temporary file before the move.
- A failed metadata copy never loses the image: the image is saved anyway and a `[WARNING]` is logged.
- When several formats are selected, a failure in one format does not prevent the others from being saved.

**Background writing:** cropping and writing happen in a separate thread, so you can review the next photo while the previous one is saved. At most 3 images wait for the disk at any time; if the disk is slower than you, the status line shows `Saving the previous images...`. At the end of the batch the program waits for the last files (`Saving the last images...`) without freezing the window.

## Metadata handling

Metadata is copied from the original scan to every output with exiftool.

| What | Copied when | Notes |
|---|---|---|
| **ICC colour profile** | Always | It defines how the colours look; dropping it would change the image |
| **EXIF and GPS** | **Save EXIF + ICC** is on | |
| **XMP and IPTC** | **Save XMP** is on | |
| **DPI override** | A DPI is forced | Applied whatever the two toggles say |
| **Manual fields** | Any field is filled | Written in a second pass, so they override copied values |

**Never copied** (they describe the uncropped scan, or are no longer true): Orientation (the pixels are already upright), the EXIF thumbnail (IFD1), ThumbnailImage, PreviewImage, ExifImageWidth, ExifImageHeight, ImageWidth, ImageHeight.

**Tags written from the Metadata tab**

| Field | Tags |
|---|---|
| Make | `EXIF:Make` |
| Model | `EXIF:Model` |
| Software (scanner) | `EXIF:Software`, `XMP:CreatorTool` |
| ISO | `EXIF:ISO` |
| Aperture | `EXIF:FNumber` |
| Shutter | `EXIF:ExposureTime` |
| Focal | `EXIF:FocalLength` |
| Film, Paper | `EXIF:UserComment`, `XMP:Description` |
| Date taken | `EXIF:DateTimeOriginal`, `EXIF:CreateDate`, `XMP:DateTimeOriginal` |
| Date digitized | `EXIF:ModifyDate`, `XMP:CreateDate` |
| Author | `EXIF:Artist`, `XMP:Creator` |
| Copyright | `EXIF:Copyright`, `XMP:Rights` |
| Latitude / Longitude | `EXIF:GPSLatitude`, `EXIF:GPSLatitudeRef`, `EXIF:GPSLongitude`, `EXIF:GPSLongitudeRef` (hemisphere chosen from the sign) |
| Altitude | `EXIF:GPSAltitude`, `EXIF:GPSAltitudeRef` (above or below sea level from the sign) |

**If exiftool is missing:** nothing is copied. The program warns you in the log only when you asked for something (copy toggles, forced DPI or manual fields), because the ICC profile alone is a silent extra. exiftool is searched in the `PATH`, and next to the executable when the app is packaged with PyInstaller.

**Time limits:** each exiftool call is limited to 60 seconds; a timeout is reported as a warning and the image is still saved.

BMP files do not carry metadata.

## How the detection works

The automatic detection finds the four corners of the print with sub-pixel accuracy:

1. **Rough location.** The scan is reduced to at most 800 pixels on the long side. The background colour is learned from a 40-pixel band around the scan border (median luminance), and a pixel counts as background if it is about as light as that median and has little colour saturation. Morphological closing and opening clean the result, and the largest region (preferably at least 5% of the image) is taken as the print.
2. **Edge band.** At full resolution, a band of about 100 pixels on each side of that region's outline is the only area examined, which ignores dust and marks elsewhere on the scanner glass.
3. **Gradients.** Sobel gradients of the smoothed image highlight the paper-to-background transition; only the strongest 4% of the gradient is kept inside the band.
4. **Straight lines.** A probabilistic Hough transform with a 0.25-degree angular resolution finds the long straight segments (at least 20% of the shorter image side, gaps up to 30 pixels).
5. **Refinement.** Each segment is replaced by a line fitted to all the edge pixels within 3 pixels of it.
6. **Grouping.** Lines are split into near-horizontal and near-vertical groups; each group is averaged (weighted by the number of supporting pixels) to get a common direction, then split into its two opposite sides at the largest gap between offsets (at least 50 pixels apart). If one side is missing, it is rebuilt parallel to the one found, using the rough bounding box.
7. **Corners.** The four lines are intersected to get the corners, ordered Top-Left, Top-Right, Bottom-Right, Bottom-Left.

If this method cannot find enough lines, a **fallback** takes over: a threshold-based segmentation on an image reduced to 900 pixels, followed by the minimum-area rectangle of the largest region. In Auto mode a fallback result is never trusted without your review.

**Straightening angle.** From the four corners the program computes two candidate angles: one from the top and bottom edges ("Horizontal") and one from the left and right edges ("Vertical"). It preselects the one with the smaller absolute value (the smaller correction). The two angles are the averages of the two parallel edges, normalised to the range from -45 to +45 degrees.

**Crop rectangle.** After rotation the corners are almost axis-aligned, and the rectangle is built from the innermost coordinate of each side. It is therefore guaranteed to lie entirely inside the print.

**What the detection assumes:** one print per scan, lying on a scanner background that differs from the paper (a white border on a white lid works because the detection also follows the gradient of the edge). For anything else, use Manual mode.

## Performance and memory

Photo Crop is tuned for the Raspberry Pi 4 with 8 GB of RAM:

- **Look-ahead loading.** While you work on one scan, the next one is read from disk and analysed in a background thread. The wait between photos is usually close to zero.
- **Single read.** Each scan is read once at its native bit depth; an 8-bit copy is used for detection, preview and zoom. For 8-bit scans both are the same array (no copy).
- **Background writer** with a bounded queue: at most 3 cropped images wait for the disk, which limits peak memory.
- **Partial rendering.** For 16-bit scans only the pixels of the crop are computed from the original.
- **Cached previews.** The full preview and the two zoom views are redrawn only when something changes, and updates are coalesced.
- **Fast metadata work.** JPEG quality is estimated from the quantisation table; the source file header is read without decoding the pixels.
- **Never blocks the interface.** Waiting for a scan or for the disk is done with timers, not with a frozen window.

## Unusual file names

A professional tool should not choke on a strange name, and Photo Crop does not:

- Every file name is listed, read and processed, whatever it contains: spaces, symbols, brackets, accented letters, emoji, or even bytes that are not valid text.
- For display, unprintable characters are replaced by a placeholder; the real name is always kept internally and never reconstructed from the displayed text.
- Names that OpenCV could not handle safely are read and written through memory buffers, so they never reach a code path that could crash the program.
- exiftool is told which character set to expect for the file name, and is given the raw bytes when necessary.

## Log messages

| Tag | Meaning |
|---|---|
| `Loaded N scan images` / `Added N scan images (T total)` | Files added to the list |
| `name from: folder` | One line per added file |
| `Output formats: ...` | The formats in force at the start of the batch, or after changing them |
| `Output folder: ...` | The destination chosen with Save to |
| `Processing [i/N]: name` | The photo being handled |
| `[OK] Successfully cropped: name` | One output file saved |
| `[WARNING]` | The file was saved, but something optional failed (for example the metadata copy), or exiftool is missing |
| `[AUTO->MANUAL]` | Auto mode did not trust the detection and queued the photo for review, with the reason |
| `[SKIPPED]` | You skipped the photo in the inspector |
| `[ERROR]` | The image could not be read |
| `[EXCEPTION]` | An unexpected error for that file; the batch continues with the next one |
| `Batch completed! Processed x/N files.` | End of the batch |

## Keyboard shortcuts

**Main window**

| Key | Action |
|---|---|
| `Enter` | Start cropping (when the button is enabled) |

**File and folder selectors**

| Key | Action |
|---|---|
| `Enter` / double-click | Open folder, or confirm file selection |
| `Right Arrow` | Enter the selected folder |
| `Left Arrow` | Go to the parent folder |
| `Esc`, `Ctrl+W`, `Ctrl+Q` | Cancel |

**Manual inspector**

| Key | Action |
|---|---|
| `1`, `2`, `3`, `4` | Select Top-Left, Top-Right, Bottom-Right, Bottom-Left corner |
| Hold several of `1` to `4` | Move several corners together |
| Arrow keys | Move the selected corner by the current step |
| `Shift` + arrow | Move by 10 pixels |
| `Shift` + `Alt` + arrow | Move by 20 pixels |
| `Z` + `Up` / `Z` + `Down` | Smaller / larger inspected area (more / less magnification) |
| `Enter` | Confirm & Crop |
| `Esc`, `Ctrl+W`, `Ctrl+Q` | Skip Image |

**Output Formats window**

| Key | Action |
|---|---|
| `Esc`, `Ctrl+W` | Cancel |

**About window**

| Key | Action |
|---|---|
| `Esc` | Close |

## Configuration file

Settings are stored in `~/.photo_crop_config.json`:

| Key | Content |
|---|---|
| `last_dir` | The last folder opened in the file selector |
| `output_dir` | The folder chosen with Save to (empty = same folder as source) |
| `save_settings` | Everything in the Output Formats window, the manual metadata and the Auto/Manual mode |
| `crop_geometry` | Size and position of the inspector window |

If the file is missing, incomplete or damaged, the program fills the gaps with the default values and keeps running. A setting from an older version that used a single "metadata" switch is automatically converted to the two current toggles (EXIF + ICC and XMP). To start from scratch, delete the file.

## Example workflows

**Fast batch of clean scans**
1. Select all the scans.
2. **Save as:** leave "Same as original".
3. Choose **Auto**, then **2. Start Cropping**.
4. Review the few photos Auto sends back to you at the end.

**Archival master plus web copy**
1. In **Save as**, turn on **TIFF** and **JPEG** (Same as original switches off).
2. TIFF tab: Bit depth `Keep original`, Method `LZW` or `Deflate`, DPI `Auto`.
3. JPEG tab: As original off, Quality 90 or 95, subsampling 4:2:0.
4. Choose **Manual**, review each photo and confirm. Both files are written from a single confirmation.

**Documenting a box of old prints**
1. In the **Metadata** tab fill Author, Copyright, Film, Paper and the digitisation date (the **Today** button helps).
2. Add Date taken and GPS for the whole batch, or leave them empty.
3. Crop; every output carries your fields on top of the metadata copied from the scan.

**Fixing the resolution of scans that lost it**
1. In the TIFF or JPEG tab turn **As original** off.
2. In the DPI panel choose the real scan resolution (for example 600).
3. Crop: the value is written to every output of that format.

**Photos scanned sideways**
- In Manual mode use **90 CW** or **90 CCW**, then check the rectangle and confirm.

## Compatibility

- Developed and tested on a **Raspberry Pi 4 (8 GB)** with Raspberry Pi OS, with mouse, keyboard and touchscreen.
- Should run on any Linux desktop that provides the dependencies listed in [System requirements](#system-requirements).
- Input: JPEG, PNG, TIFF, BMP (8 and 16 bit). Output: JPEG, TIFF, PNG, and BMP only through "Same as original".
- TIFF compression selection requires an OpenCV build that exposes the TIFF compression option; everything else works without it.
- The code is prepared to run as a PyInstaller bundle (assets and exiftool are looked up next to the executable).

## Notes and troubleshooting

- **The crop is slightly smaller than the paper.** This is intentional: the rectangle is the largest one that fits inside the straightened print, so you never get a sliver of scanner background at the edges. In Manual mode you can move any edge.
- **Auto sent many photos to manual review.** The print probably blends with the scanner background, or touches the scan border. Scan with a small margin around each print, or work in Manual mode.
- **The detection misses the edge.** Use Manual mode: drag the corners on the preview, then refine with the zoom views. **Reset Auto Rect** brings the detected rectangle back.
- **"exiftool not found" in the log.** Install it with `sudo apt install libimage-exiftool-perl`. Until then DPI, ICC profile, EXIF and manual metadata are not written.
- **The TIFF compression slider is disabled.** Your OpenCV build cannot set it; TIFF files are still saved with the library default.
- **16-bit scan saved as JPEG looks different in file size.** JPEG is 8-bit only; the conversion happens automatically.
- **The menu entry is not visible.** Log out and back in, or reboot.
- **Font looks different.** Install `fonts-inter`.
- **Settings behave strangely.** Delete `~/.photo_crop_config.json` to restore defaults.

## Built with

- [Python 3](https://www.python.org/) and Tkinter
- [OpenCV](https://opencv.org/) for image reading, detection, rotation and writing
- [NumPy](https://numpy.org/)
- [Pillow](https://python-pillow.org/) for JPEG writing, header inspection and the interface images
- [ExifTool](https://exiftool.org/) by Phil Harvey for metadata
- [Inter](https://rsms.me/inter/) typeface

## Author

**Flavio Scutti**, 2026.

## License

This project is released under the GNU General Public License v3.0. See the [LICENSE](LICENSE) file for details.
