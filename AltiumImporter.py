"""
Altium PCB Importer - Fusion 360 add-in.

Adds "Import Altium PCB" to Design > Solid > Insert. Opens a binary
Altium .PcbDoc and builds a 3D model: extruded board with holes and cutouts,
plus components (embedded STEP models, Altium extruded 3D bodies, or
placeholder boxes from the footprint pads).
"""

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
import layer_geometry  # noqa: E402
import fusion_layers  # noqa: E402
import fusion_builder  # noqa: E402

for _m in (altium_pcbdoc, layer_geometry, fusion_layers, fusion_builder):
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
            inputs.addBoolValueInput("new_doc", "Create in a new design", True, "", True)
        except Exception:
            _err("Failed to start import:\n")


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
