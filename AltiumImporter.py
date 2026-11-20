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
_state = {"doc": None, "path": None}
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


def _custom_visible(inputs):
    dd = inputs.itemById("colors")
    custom = dd is not None and dd.selectedItem is not None and \
        dd.selectedItem.name == CUSTOM_COLORS
    for cid in ("mask_color", "silk_color"):
        ci = inputs.itemById(cid)
        if ci:
            ci.isVisible = custom


def _err(prefix=""):
    if _ui:
        _ui.messageBox(prefix + traceback.format_exc(), CMD_NAME)


def _pick_file():
    dlg = _ui.createFileDialog()
    dlg.title = "Open Altium PCB"
    dlg.filter = "Altium PCB (*.PcbDoc;*.pcbdoc);;All files (*.*)"
    dlg.isMultiSelectEnabled = False
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
            _state["doc"] = None

            path = _pick_file()
            doc = None
            if path:
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
            origin = inputs.addDropDownCommandInput(
                "origin", "Origin", adsk.core.DropDownStyles.TextListDropDownStyle)
            origin.listItems.add("Altium board origin", True)
            origin.listItems.add("Board lower-left corner", False)
            origin.listItems.add("Altium absolute origin", False)
            inputs.addBoolValueInput("pad_holes", "Pad holes / slots", True, "", True)
            inputs.addBoolValueInput("via_holes", "Via holes (slow on dense boards)", True, "", False)
            inputs.addBoolValueInput("cutouts", "Board cutouts", True, "", True)
            inputs.addBoolValueInput("step_models", "Embedded STEP models", True, "", True)
            inputs.addBoolValueInput("extruded", "Altium extruded 3D bodies", True, "", True)
            inputs.addBoolValueInput("placeholders", "Boxes for parts without a body", True, "", True)
            inputs.addValueInput("ph_height", "Default box height", "mm",
                                 adsk.core.ValueInput.createByReal(0.1))
            inputs.addBoolValueInput("copper", "Outer copper layers", True, "", True)
            inputs.addBoolValueInput("mask", "Solder mask", True, "", True)
            inputs.addBoolValueInput("silk", "Silkscreen", True, "", True)
            inputs.addBoolValueInput("silk_text", "Silkscreen text", True, "", True)
            prefs = _load_settings()
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
            _custom_visible(inputs)
            inputs.addBoolValueInput("new_doc", "Create in a new design", True, "", True)
        except Exception:
            _err("Failed to start import:\n")


class InputChangedHandler(adsk.core.InputChangedEventHandler):
    def notify(self, args):
        try:
            if args.input.id == "colors":
                _custom_visible(args.inputs)
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
            if not doc:
                return
            inp = args.command.commandInputs
            opt = fusion_builder.Options()
            opt.thickness_mm = inp.itemById("thickness").value * 10.0
            opt.origin = ["altium", "board", "absolute"][inp.itemById("origin").selectedItem.index]
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
