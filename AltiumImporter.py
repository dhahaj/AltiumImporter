"""
Altium PCB Importer - Fusion 360 add-in.

Adds "Import Altium PCB" to Design > Solid > Insert. Opens a binary
Altium .PcbDoc and builds a 3D model: extruded board with holes and cutouts,
plus components (embedded STEP models, Altium extruded 3D bodies, or
placeholder boxes from the footprint pads).
"""

import json
import os
import sys
import traceback

import adsk.core
import adsk.fusion

_HERE = os.path.dirname(os.path.abspath(__file__))
_LIB = os.path.join(_HERE, "lib")
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

import importlib  # noqa: E402
import altium_pcbdoc  # noqa: E402
import font_metrics  # noqa: E402
import layer_geometry  # noqa: E402
import fusion_layers  # noqa: E402
import fusion_builder  # noqa: E402

for _m in (altium_pcbdoc, font_metrics, layer_geometry, fusion_layers, fusion_builder):
    importlib.reload(_m)

CMD_ID = "altiumImporter_importPcbDoc"
CMD_NAME = "Import Altium PCB"
CMD_TIP = "Build a 3D model from an Altium Designer .PcbDoc file"
PANEL_ID = "InsertPanel"
WORKSPACE_ID = "FusionSolidEnvironment"

_app = None
_ui = None
_handlers = []
_state = {"doc": None, "path": None, "inputs": None, "html_colors": None}
_SETTINGS = os.path.join(_HERE, "settings.json")


def _load_settings():
    try:
        with open(_SETTINGS, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_settings(**kw):
    data = _load_settings()
    data.update(kw)
    try:
        with open(_SETTINGS, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)
    except Exception:
        pass   # read-only install folder etc.: just don't remember


CUSTOM_COLORS = "Custom..."


PICKER_HTML = os.path.join(_HERE, "html", "color_picker.html")


def _custom_visible(inputs):
    dd = inputs.itemById("colors")
    custom = dd is not None and dd.selectedItem is not None and \
        dd.selectedItem.name == CUSTOM_COLORS
    for cid in ("mask_color", "silk_color", "color_picker"):
        ci = inputs.itemById(cid)
        if ci:
            ci.isVisible = custom


def _picker_colors(inputs):
    """Current custom colours as {'mask': '#RRGGBB', 'silk': '#RRGGBB'}."""
    out = {}
    for key, cid, default in (("mask", "mask_color", (26, 107, 45)),
                              ("silk", "silk_color", (245, 245, 240))):
        ci = inputs.itemById(cid) if inputs else None
        rgb = fusion_builder.parse_color(ci.value) if ci else None
        out[key] = fusion_builder.color_hex(rgb or default)
    return out


def _push_to_picker(inputs):
    """Send the hex boxes' colours to the picker, unless it sent them itself."""
    bi = inputs.itemById("color_picker") if inputs else None
    if bi is None:
        return
    cols = _picker_colors(inputs)
    if cols != _state.get("html_colors"):
        _state["html_colors"] = cols
        bi.sendInfoToHTML("set", json.dumps(cols))


def _err(prefix=""):
    if _ui:
        _ui.messageBox(prefix + traceback.format_exc(), CMD_NAME)


# (id, label, default) for the dialog's check boxes, in display order.
# The defaults apply until an import has been run; after that the dialog
# opens with whatever was used last (saved in settings.json).
BOOL_OPTIONS = [
    ("pad_holes", "Pad holes / slots", True),
    ("via_holes", "Via holes (slow on dense boards)", False),
    ("cutouts", "Board cutouts", True),
    ("step_models", "Embedded STEP models", True),
    ("extruded", "Altium extruded 3D bodies", True),
    ("placeholders", "Boxes for parts without a body", True),
    ("copper", "Outer copper layers", True),
    ("mask", "Solder mask", True),
    ("silk", "Silkscreen", True),
    ("silk_text", "Silkscreen text", True),
    ("new_doc", "Create in a new design", True),
]
ORIGINS = [("altium", "Altium board origin"),
           ("board", "Board lower-left corner"),
           ("absolute", "Altium absolute origin")]


def _pick_file(start_dir=None):
    dlg = _ui.createFileDialog()
    dlg.title = "Open Altium PCB"
    dlg.filter = "Altium PCB (*.PcbDoc;*.pcbdoc);;All files (*.*)"
    dlg.isMultiSelectEnabled = False
    if start_dir and os.path.isdir(start_dir):
        dlg.initialDirectory = start_dir
    if dlg.showOpen() != adsk.core.DialogResults.DialogOK:
        return None
    return dlg.filename


class CreatedHandler(adsk.core.CommandCreatedEventHandler):
    def notify(self, args):
        try:
            cmd = args.command
            on_exec = ExecuteHandler()
            cmd.execute.add(on_exec)
            _handlers.append(on_exec)
            on_changed = InputChangedHandler()
            cmd.inputChanged.add(on_changed)
            _handlers.append(on_changed)
            on_validate = ValidateHandler()
            cmd.validateInputs.add(on_validate)
            _handlers.append(on_validate)
            on_html = PickerHandler()
            cmd.incomingFromHTML.add(on_html)
            _handlers.append(on_html)
            _state["doc"] = None

            prefs = _load_settings()
            path = _pick_file(prefs.get("last_folder"))
            doc = None
            if path:
                _save_settings(last_folder=os.path.dirname(path))
                try:
                    doc = altium_pcbdoc.read_pcbdoc(path)
                except altium_pcbdoc.AltiumParseError as e:
                    _ui.messageBox(str(e), CMD_NAME)
            if not doc:
                cmd.isAutoExecute = True   # close silently, nothing to do
                return
            _state["doc"], _state["path"] = doc, path

            inputs = cmd.commandInputs
            inputs.addTextBoxCommandInput("info", "File",
                                          "<b>%s</b><br>%s" % (os.path.basename(path), doc.summary()),
                                          3, True)
            inputs.addValueInput("thickness", "Board thickness", "mm",
                                 adsk.core.ValueInput.createByReal(doc.thickness / 10.0))
            saved = prefs.get("options", {})
            if not isinstance(saved, dict):
                saved = {}

            def flag(oid, default):
                v = saved.get(oid, default)
                return v if isinstance(v, bool) else default

            origin = inputs.addDropDownCommandInput(
                "origin", "Origin", adsk.core.DropDownStyles.TextListDropDownStyle)
            last_origin = saved.get("origin", ORIGINS[0][0])
            if last_origin not in [o[0] for o in ORIGINS]:
                last_origin = ORIGINS[0][0]
            for key, label in ORIGINS:
                origin.listItems.add(label, key == last_origin)
            bools = dict((oid, (label, default)) for oid, label, default in BOOL_OPTIONS)

            def add_bool(oid):
                label, default = bools[oid]
                inputs.addBoolValueInput(oid, label, True, "", flag(oid, default))

            for oid in ("pad_holes", "via_holes", "cutouts", "step_models", "extruded",
                        "placeholders"):
                add_bool(oid)
            ph = saved.get("ph_height_mm", 1.0)
            if not isinstance(ph, (int, float)) or not 0 < ph < 100:
                ph = 1.0
            inputs.addValueInput("ph_height", "Default box height", "mm",
                                 adsk.core.ValueInput.createByReal(ph / 10.0))
            for oid in ("copper", "mask", "silk", "silk_text"):
                add_bool(oid)
            last = prefs.get("colors", fusion_builder.COLOR_PRESETS[0][0])
            names = [p[0] for p in fusion_builder.COLOR_PRESETS] + [CUSTOM_COLORS]
            if last not in names:
                last = names[0]
            colors = inputs.addDropDownCommandInput(
                "colors", "Mask / silkscreen color", adsk.core.DropDownStyles.TextListDropDownStyle)
            for n in names:
                colors.listItems.add(n, n == last)
            colors.tooltip = "Solder mask color / silkscreen color"
            mc = inputs.addStringValueInput("mask_color", "Mask color",
                                            prefs.get("mask_color", "#1A6B2D"))
            mc.tooltip = "Hex (#1A6B2D) or R,G,B (26,107,45)"
            sc = inputs.addStringValueInput("silk_color", "Silkscreen color",
                                            prefs.get("silk_color", "#F5F5F0"))
            sc.tooltip = "Hex (#F5F5F0) or R,G,B (245,245,240)"
            _state["inputs"], _state["html_colors"] = inputs, None
            if os.path.isfile(PICKER_HTML):
                try:
                    # Fusion prefixes "file:///" itself and URL-encodes backslashes
                    # (ERR_INVALID_URL), so hand it a forward-slash path.
                    bi = inputs.addBrowserCommandInput("color_picker", "",
                                                       PICKER_HTML.replace("\\", "/"), 300, 300)
                    bi.tooltip = "Pick the mask or silkscreen color"
                except Exception:
                    pass   # older Fusion without browser inputs: hex boxes still work
            _custom_visible(inputs)
            add_bool("new_doc")
        except Exception:
            _err("Failed to start import:\n")


class InputChangedHandler(adsk.core.InputChangedEventHandler):
    def notify(self, args):
        try:
            if args.input.id == "colors":
                _custom_visible(args.inputs)
            elif args.input.id in ("mask_color", "silk_color"):
                if fusion_builder.parse_color(args.input.value):
                    _push_to_picker(args.inputs)
        except Exception:
            pass


class PickerHandler(adsk.core.HTMLEventHandler):
    """Messages from html/color_picker.html."""
    def notify(self, args):
        try:
            inputs = _state.get("inputs")
            if args.action == "ready":
                cols = _picker_colors(inputs)
                _state["html_colors"] = cols
                args.returnData = json.dumps(cols)
            elif args.action == "colors":
                cols = json.loads(args.data)
                new = {}
                for key, cid in (("mask", "mask_color"), ("silk", "silk_color")):
                    rgb = fusion_builder.parse_color(cols.get(key))
                    if rgb:
                        new[key] = fusion_builder.color_hex(rgb)
                old = _picker_colors(inputs)
                _state["html_colors"] = dict(old, **new)
                for key, cid in (("mask", "mask_color"), ("silk", "silk_color")):
                    if key in new and new[key] != old[key]:
                        inputs.itemById(cid).value = new[key]
                args.returnData = "OK"
        except Exception:
            pass


class ValidateHandler(adsk.core.ValidateInputsEventHandler):
    def notify(self, args):
        try:
            inputs = args.inputs
            dd = inputs.itemById("colors")
            if dd and dd.selectedItem and dd.selectedItem.name == CUSTOM_COLORS:
                ok = all(fusion_builder.parse_color(inputs.itemById(c).value)
                         for c in ("mask_color", "silk_color"))
                args.areInputsValid = ok
        except Exception:
            pass


class ExecuteHandler(adsk.core.CommandEventHandler):
    def notify(self, args):
        progress = None
        try:
            doc = _state["doc"]
            _state["inputs"] = None
            if not doc:
                return
            inp = args.command.commandInputs
            opt = fusion_builder.Options()
            opt.thickness_mm = inp.itemById("thickness").value * 10.0
            opt.origin = ORIGINS[inp.itemById("origin").selectedItem.index][0]
            opt.pad_holes = inp.itemById("pad_holes").value
            opt.via_holes = inp.itemById("via_holes").value
            opt.cutouts = inp.itemById("cutouts").value
            opt.step_models = inp.itemById("step_models").value
            opt.extruded_bodies = inp.itemById("extruded").value
            opt.placeholders = inp.itemById("placeholders").value
            opt.placeholder_height_mm = inp.itemById("ph_height").value * 10.0
            opt.copper = inp.itemById("copper").value
            opt.mask = inp.itemById("mask").value
            opt.silk = inp.itemById("silk").value
            opt.silk_text = inp.itemById("silk_text").value
            opt.new_document = inp.itemById("new_doc").value
            _save_settings(options=dict(
                [(oid, inp.itemById(oid).value) for oid, _, _ in BOOL_OPTIONS] +
                [("origin", opt.origin), ("ph_height_mm", opt.placeholder_height_mm)]))
            choice = inp.itemById("colors").selectedItem.name
            if choice == CUSTOM_COLORS:
                m_txt = inp.itemById("mask_color").value
                s_txt = inp.itemById("silk_color").value
                opt.mask_rgb = fusion_builder.parse_color(m_txt) or opt.mask_rgb
                opt.silk_rgb = fusion_builder.parse_color(s_txt) or opt.silk_rgb
                opt.mask_opacity = 0.94
                _save_settings(colors=choice, mask_color=m_txt, silk_color=s_txt)
            else:
                for name, mask_rgb, silk_rgb, opacity in fusion_builder.COLOR_PRESETS:
                    if name == choice:
                        opt.mask_rgb, opt.silk_rgb, opt.mask_opacity = mask_rgb, silk_rgb, opacity
                _save_settings(colors=choice)

            progress = _ui.createProgressDialog()
            progress.cancelButtonText = "Cancel"
            progress.isCancelButtonShown = True
            progress.show(CMD_NAME, "Starting...", 0, 100, 0)

            name = os.path.splitext(os.path.basename(_state["path"]))[0]
            builder = fusion_builder.Builder(doc, opt, name, progress)
            log = builder.build()
            progress.hide()
            msg = "\n".join([doc.summary(), ""] + log[:25] + doc.warnings[:10])
            if len(log) > 25:
                msg += "\n... %d more messages" % (len(log) - 25)
            _ui.messageBox(msg, CMD_NAME + " - done")
        except KeyboardInterrupt:
            if progress:
                progress.hide()
            _ui.messageBox("Import cancelled. Partial geometry was kept.", CMD_NAME)
        except Exception:
            if progress:
                progress.hide()
            _err("Import failed:\n")


def run(context):
    global _app, _ui
    try:
        _app = adsk.core.Application.get()
        _ui = _app.userInterface
        defs = _ui.commandDefinitions
        cmd_def = defs.itemById(CMD_ID)
        if cmd_def:
            cmd_def.deleteMe()
        res = os.path.join(_HERE, "resources")
        cmd_def = defs.addButtonDefinition(CMD_ID, CMD_NAME, CMD_TIP,
                                           res if os.path.isdir(res) else "")
        on_created = CreatedHandler()
        cmd_def.commandCreated.add(on_created)
        _handlers.append(on_created)

        ws = _ui.workspaces.itemById(WORKSPACE_ID)
        panel = ws.toolbarPanels.itemById(PANEL_ID) if ws else None
        if panel and not panel.controls.itemById(CMD_ID):
            ctrl = panel.controls.addCommand(cmd_def)
            ctrl.isPromoted = True
    except Exception:
        _err("Altium importer failed to load:\n")


def stop(context):
    try:
        ws = _ui.workspaces.itemById(WORKSPACE_ID)
        panel = ws.toolbarPanels.itemById(PANEL_ID) if ws else None
        if panel:
            ctrl = panel.controls.itemById(CMD_ID)
            if ctrl:
                ctrl.deleteMe()
        cmd_def = _ui.commandDefinitions.itemById(CMD_ID)
        if cmd_def:
            cmd_def.deleteMe()
        _handlers.clear()
    except Exception:
        pass
