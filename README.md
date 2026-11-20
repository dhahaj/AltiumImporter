# Altium PCB Importer for Fusion 360

A Fusion 360 add-in that opens an Altium Designer **binary `.PcbDoc`** directly and builds a 3D model of the board. You don't need to export STEP or IDF first, and there's nothing to `pip install`.

## What it builds

| Altium data | Fusion result |
|---|---|
| Board shape (Board6 outline, arcs included) | Extruded board body, thickness taken from the layer stackup (you can override it) |
| Board cutouts (regions marked *Board Cutout*) | Cut through the board |
| Pad holes and slots | Cut through the board |
| Via holes | Optional, off by default because dense boards get slow |
| Components with **embedded STEP models** | STEP imported once per model, then placed as linked occurrences |
| Components with Altium **extruded 3D bodies** | Extruded with the body's stand-off and overall height and its 3D color |
| Components with no 3D body | Placeholder box from the pad extents, using the component's Height (or a default) |
| Top and bottom copper (pads, tracks, arcs, fills, polygon pours, solid regions) | Gold copper bodies, drilled through |
| Top and bottom solder mask | Green mask body with openings from the pad and via mask expansion (rules, manual values and tenting are honoured) |
| Top and bottom overlay (silkscreen lines, arcs, fills and text) | White silkscreen body, clipped off the pads like a fab would |

Everything goes under one top-level component named after the file:

```
<PcbDoc name>
├── Board        (PCB core, Top/Bottom copper, Top/Bottom copper pour N,
│                 Top/Bottom solder mask, Top/Bottom silkscreen, sketches)
└── Components
    ├── U1 (SOIC127P620X175-8M)
    ├── C12 (CAPC1608X09N)
    └── ...
```

The bottom of the board sits on Z = 0, and the top copper is at Z = board thickness. The thin layers stack like this (cu = outer copper thickness from the stackup):

| Layer | Top side | Bottom side |
|---|---|---|
| Copper | thickness − cu … thickness | 0 … cu |
| Solder mask (0.02 mm) | thickness − cu … thickness + 0.02 | −0.02 … cu |
| Silkscreen (0.015 mm) | on top of the mask | below the mask |

With copper on, the FR-4 core fills the space between the two copper layers, so the board's overall copper-to-copper thickness still matches the stackup. Mask and silk add a few hundredths of a millimetre on each side, as on a real board.

## Install

1. Download or clone this folder. Keep the folder name `AltiumImporter`. It has to contain `AltiumImporter.py`, `AltiumImporter.manifest` and `lib/`.
2. In Fusion: **Utilities → Add-Ins → Scripts and Add-Ins** (or press **Shift+S**).
3. On the **Add-Ins** tab, click the **+** next to *My Add-Ins*, choose **Script or add-in from device**, and select the `AltiumImporter` folder.
4. Select **AltiumImporter**, click **Run**, and tick **Run on Startup** if you want it loaded every time.

You can also copy the folder into Fusion's add-in directory and restart Fusion:
- Windows: `%APPDATA%\Autodesk\Autodesk Fusion 360\API\AddIns\`
- macOS: `~/Library/Application Support/Autodesk/Autodesk Fusion 360/API/AddIns/`

## Use

1. In the **Design** workspace, go to **Solid → Insert → Import Altium PCB**.
2. Pick a `.PcbDoc` file. The dialog shows what was found: board size, thickness, and counts of components, pads, vias and embedded models.
3. Adjust the options and click **OK**:
   - **Board thickness**: prefilled from the stackup (copper plus dielectric).
   - **Origin**: the Altium board origin (the default), the board's lower-left corner, or the Altium absolute origin.
   - **Pad holes / Via holes / Board cutouts**
   - **Embedded STEP models / Altium extruded 3D bodies / Boxes for parts without a body**
   - **Outer copper layers / Solder mask / Silkscreen / Silkscreen text**: all on by default. Turn them off for a plain mechanical model; they add roughly 30 to 90 seconds on a typical board.
   - **Create in a new design**: on by default. The new design uses direct modeling (no timeline), which is much faster with hundreds of parts.

You can cancel from the progress dialog at any time, and the geometry built so far stays in the design.

## Tips for Altium users

- **Embed your models.** Only STEP models that are *embedded* in the PcbDoc can be imported. If your 3D bodies are linked to external STEP files, run **Tools → Manage 3D Bodies for Components on Board** or place the footprints from a library with embedded models before saving. Linked bodies fall back to extruded bodies or placeholder boxes.
- **Save as binary.** ASCII-format PcbDocs aren't supported.
- **For panels**, import the single-board PcbDoc. Embedded board arrays aren't expanded.

## Limitations

- Only the outer copper layers are modelled. Inner layers, paste and mechanical layers are skipped.
- Polygon pours use the poured shape Altium saved in the file. Repour in Altium before saving if the pours are stale.
- Silkscreen text uses Arial for Altium's stroke font and the named font for TrueType text, so letter shapes and widths differ slightly from Altium. Special strings that can't be resolved (such as `.Layer_Name`) are skipped, and inverted or barcode text is drawn as plain text.
- Solder mask is shown slightly transparent so you can see the copper underneath, like a real green mask. To keep imports fast, the mask body overlaps the copper instead of being cut around it, and large polygon pours are kept as separate "copper pour" bodies. That looks the same but matters if you measure volumes or run interference checks; hide the mask and copper bodies for those.
- Rectangular (square) holes are drilled as round holes.
- Bottom-side STEP models are flipped about the board Y axis. That matches Altium's left/right mirroring, but models with a non-zero base X/Y rotation on the bottom side haven't been verified against Altium on many parts yet. Please report any that end up rotated wrong.
- Self-intersecting extruded body outlines fall back to their bounding box.

## Project layout

```
AltiumImporter/
├── AltiumImporter.py        # add-in entry point: command, dialog, progress
├── AltiumImporter.manifest
└── lib/
    ├── altium_pcbdoc.py     # pure-Python PcbDoc reader (no Fusion dependency)
    ├── layer_geometry.py    # pure-Python copper/mask/silk shapes per layer
    ├── fusion_builder.py    # turns the parsed board into Fusion geometry
    ├── fusion_layers.py     # fast solid modelling for the thin layers
    ├── olefile.py           # vendored OLE2 reader (BSD, © Philippe Lagadec)
    └── olefile_LICENSE.txt
```

`altium_pcbdoc.py` also runs on its own under any Python 3, which is handy for checking a file:

```
python lib/altium_pcbdoc.py MyBoard.PcbDoc
  Board 40.00 x 100.00 mm, 1.551 mm thick, 0 cutouts, 157 components, 468 pads, 321 vias, 194 bodies (113 embedded STEP), 23 models
```

## Credits

The record layouts follow the reverse-engineering in KiCad's Altium importer (`pcbnew/pcb_io/altium`). This is an independent Python implementation. Sample boards for testing came from KiCad's QA data.
