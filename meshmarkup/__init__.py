# SPDX-License-Identifier: GPL-3.0-or-later
"""
MeshMarkup for Blender: send the scene (or the selection) to your client as a
3D review link. They open it in a browser and click the model to leave notes,
pinned where they mean. Send it again later as the next version, with the open
notes carried over.

Nothing is sent without a click, and nothing goes online while Blender's
"Allow Online Access" is off. The GLB export also works on its own, offline.
"""
import json
import os
import tempfile
import textwrap
import threading
import time

import bpy
import bpy.utils.previews
from bpy.props import BoolProperty, EnumProperty, FloatProperty, IntProperty, StringProperty

from . import api

DEFAULT_SERVER = "https://meshmarkup.com"
_icons = None
_job = {}          # the send running in the background: progress, stage, result
_enum_cache = []   # Blender needs dynamic enum strings kept alive on the Python side


# ------------------------------------------------------------------ helpers
def prefs(context=None):
    return (context or bpy.context).preferences.addons[__package__].preferences


def icon(name="meshmarkup"):
    return _icons[name].icon_id if _icons and name in _icons else 0


def export_dir():
    """Where the GLB is written before sending: the extension's own user folder."""
    try:
        return bpy.utils.extension_path_user(__package__, path="exports", create=True)
    except Exception:  # running outside the extensions system (development)
        return tempfile.gettempdir()


def blend_name():
    return bpy.path.display_name_from_filepath(bpy.data.filepath) or "Untitled"


def wrap(layout, text, width_px, icon_name="NONE", alert=False):
    """Blender labels don't wrap: split the text to the panel's width."""
    col = layout.column(align=True)
    col.alert = alert
    chars = max(24, int(width_px / 7.2))
    for i, line in enumerate(textwrap.wrap(text, chars)):
        col.label(text=line, icon=icon_name if i == 0 else "BLANK1" if icon_name != "NONE" else "NONE")
    return col


def redraw():
    for win in bpy.context.window_manager.windows:
        for area in win.screen.areas:
            if area.type in {"VIEW_3D", "PREFERENCES"}:
                area.tag_redraw()


def projects_items(self, context):
    try:
        items = json.loads(prefs(context).projects or "[]")
    except Exception:
        items = []
    global _enum_cache
    _enum_cache = [(p["id"], p["name"], "") for p in items] or [("DEFAULT", "First project", "")]
    return _enum_cache


def remember(entry):
    p = prefs()
    try:
        recent = json.loads(p.recent or "[]")
    except Exception:
        recent = []
    recent = [r for r in recent if not (r.get("asset") == entry["asset"] and r.get("version") == entry["version"])]
    p.recent = json.dumps(([entry] + recent)[:6])


# -------------------------------------------------------------- preferences
class MESHMARKUP_Preferences(bpy.types.AddonPreferences):
    bl_idname = __package__

    token: StringProperty(name="Token", default="", subtype="PASSWORD", options={"HIDDEN"})
    studio: StringProperty(name="Studio", default="")
    plan: StringProperty(name="Plan", default="")
    projects: StringProperty(default="[]", options={"HIDDEN"})
    recent: StringProperty(default="[]", options={"HIDDEN"})
    show_advanced: BoolProperty(name="Advanced", default=False)
    server: StringProperty(name="Server", default=DEFAULT_SERVER,
                           description="Only change this when testing against another MeshMarkup server")

    def draw(self, context):
        layout = self.layout
        if self.token:
            row = layout.row()
            row.label(text=f"Connected to {self.studio or 'MeshMarkup'}", icon="CHECKMARK")
            row.operator("meshmarkup.disconnect", icon="X")
        else:
            draw_intro(layout, context)
        box = layout.box()
        box.prop(self, "show_advanced", icon="TRIA_DOWN" if self.show_advanced else "TRIA_RIGHT", emboss=False)
        if self.show_advanced:
            box.prop(self, "server")


# --------------------------------------------------------------- scene data
def register_props():
    S = bpy.types.Scene
    S.meshmarkup_name = StringProperty(name="Name", description="The model's name in MeshMarkup (defaults to the .blend file name)")
    S.meshmarkup_selection = BoolProperty(name="Selection only", description="Send only the selected objects", default=False)
    S.meshmarkup_project = EnumProperty(name="Project", description="Where a new model goes", items=projects_items)
    # What this .blend was last sent as, so the next send can be its next version.
    S.meshmarkup_asset_id = StringProperty(options={"HIDDEN"})
    S.meshmarkup_asset_name = StringProperty(options={"HIDDEN"})
    S.meshmarkup_version = IntProperty(options={"HIDDEN"})
    S.meshmarkup_review_url = StringProperty(options={"HIDDEN"})
    S.meshmarkup_model_url = StringProperty(options={"HIDDEN"})
    S.meshmarkup_carried = IntProperty(options={"HIDDEN"})
    W = bpy.types.WindowManager
    W.meshmarkup_code = StringProperty(options={"SKIP_SAVE"})
    W.meshmarkup_busy = BoolProperty(options={"SKIP_SAVE"})
    W.meshmarkup_stage = StringProperty(options={"SKIP_SAVE"})
    W.meshmarkup_progress = FloatProperty(options={"SKIP_SAVE"}, min=0, max=1)
    W.meshmarkup_error = StringProperty(options={"SKIP_SAVE"})


def unregister_props():
    for name in ("meshmarkup_name", "meshmarkup_selection", "meshmarkup_project", "meshmarkup_asset_id",
                 "meshmarkup_asset_name", "meshmarkup_version", "meshmarkup_review_url",
                 "meshmarkup_model_url", "meshmarkup_carried"):
        if hasattr(bpy.types.Scene, name):
            delattr(bpy.types.Scene, name)
    for name in ("meshmarkup_code", "meshmarkup_busy", "meshmarkup_stage", "meshmarkup_progress", "meshmarkup_error"):
        if hasattr(bpy.types.WindowManager, name):
            delattr(bpy.types.WindowManager, name)


# ------------------------------------------------------------- connecting
class MESHMARKUP_OT_connect(bpy.types.Operator):
    """Connect Blender to your MeshMarkup studio. You approve a code in your browser; no password goes through Blender"""
    bl_idname = "meshmarkup.connect"
    bl_label = "Connect to MeshMarkup"

    _timer = None

    def invoke(self, context, event):
        p = prefs(context)
        wm = context.window_manager
        try:
            status, j = api.request(p.server, "POST", "/api/addon/connect/start", {"app": "blender"})
        except api.Offline:
            self.report({"ERROR"}, "Online access is off. Turn on Allow Online Access first")
            return {"CANCELLED"}
        if status != 200:
            self.report({"ERROR"}, j.get("error", "Couldn't reach MeshMarkup"))
            return {"CANCELLED"}
        self._poll = j["poll"]
        self._until = time.time() + int(j.get("expiresIn", 600))
        self._interval = int(j.get("interval", 3))
        self._next = time.time() + self._interval
        wm.meshmarkup_code = j["code"]
        wm.meshmarkup_error = ""
        bpy.ops.wm.url_open(url=j["url"])
        self._timer = wm.event_timer_add(0.5, window=context.window)
        wm.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def modal(self, context, event):
        wm = context.window_manager
        if not wm.meshmarkup_code:                       # cancelled from the panel
            return self._finish(context, None)
        if event.type != "TIMER" or time.time() < self._next:
            return {"PASS_THROUGH"}
        self._next = time.time() + self._interval
        if time.time() > self._until:
            return self._finish(context, "The code expired. Click Connect again.")
        p = prefs(context)
        try:
            status, j = api.request(p.server, "POST", "/api/addon/connect/poll", {"poll": self._poll}, timeout=15)
        except api.Offline:
            return self._finish(context, "Online access was turned off.")
        if status == 200 and j.get("status") == "approved":
            p.token = j["token"]
            p.studio = j.get("studio", "")
            refresh_account(p)
            self.report({"INFO"}, f"Connected to {p.studio or 'MeshMarkup'}")
            return self._finish(context, None)
        if status in (404, 410):
            return self._finish(context, "That code expired or was already used. Click Connect again.")
        return {"PASS_THROUGH"}

    def _finish(self, context, error):
        wm = context.window_manager
        if self._timer:
            wm.event_timer_remove(self._timer)
        wm.meshmarkup_code = ""
        if error:
            wm.meshmarkup_error = error
        redraw()
        return {"FINISHED"} if not error else {"CANCELLED"}


class MESHMARKUP_OT_cancel_connect(bpy.types.Operator):
    """Stop waiting for the browser"""
    bl_idname = "meshmarkup.cancel_connect"
    bl_label = "Cancel"

    def execute(self, context):
        context.window_manager.meshmarkup_code = ""
        return {"FINISHED"}


def refresh_account(p):
    """Studio, plan and projects, for the panel. Called only from a click (connect, refresh)."""
    status, j = api.request(p.server, "GET", "/api/addon/whoami", token=p.token, timeout=15)
    if status == 401:
        p.token = ""
        return j.get("error", "This add-on was disconnected. Connect it again.")
    if status != 200:
        return j.get("error", "Couldn't reach MeshMarkup.")
    p.studio = j.get("studio", p.studio)
    p.plan = j.get("planLabel", "")
    p.projects = json.dumps(j.get("projects", []))
    return None


class MESHMARKUP_OT_refresh(bpy.types.Operator):
    """Update the studio, plan and project list from MeshMarkup"""
    bl_idname = "meshmarkup.refresh"
    bl_label = "Refresh"

    def execute(self, context):
        try:
            err = refresh_account(prefs(context))
        except api.Offline:
            err = "Online access is off."
        context.window_manager.meshmarkup_error = err or ""
        return {"FINISHED"}


class MESHMARKUP_OT_disconnect(bpy.types.Operator):
    """Forget the MeshMarkup connection on this computer"""
    bl_idname = "meshmarkup.disconnect"
    bl_label = "Disconnect"

    def execute(self, context):
        p = prefs(context)
        p.token = p.studio = p.plan = ""
        p.projects = "[]"
        return {"FINISHED"}


# ---------------------------------------------------------------- export
def export_glb(context, path, selection_only):
    bpy.ops.export_scene.gltf(
        filepath=path, export_format="GLB", use_selection=selection_only,
        export_apply=True,          # modifiers applied: the client sees what you see
        export_yup=True,
    )
    return os.path.getsize(path)


class MESHMARKUP_OT_export_glb(bpy.types.Operator):
    """Save a review-ready GLB (modifiers applied, Y up) anywhere. Works offline, no account needed"""
    bl_idname = "meshmarkup.export_glb"
    bl_label = "Save review-ready GLB"

    filepath: StringProperty(subtype="FILE_PATH")
    filter_glob: StringProperty(default="*.glb", options={"HIDDEN"})

    def invoke(self, context, event):
        self.filepath = bpy.path.ensure_ext(os.path.join(os.path.dirname(bpy.data.filepath) or os.path.expanduser("~"),
                                                          bpy.path.clean_name(context.scene.meshmarkup_name or blend_name())), ".glb")
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        path = bpy.path.ensure_ext(self.filepath, ".glb")
        size = export_glb(context, path, context.scene.meshmarkup_selection)
        self.report({"INFO"}, f"Saved {os.path.basename(path)} ({size / 1024 ** 2:.1f} MB)")
        return {"FINISHED"}


# ------------------------------------------------------------------ send
def _send_worker(server, token, body, glb):
    """Runs in a background thread: never touches bpy. Writes progress into _job."""
    try:
        _job.update(stage="Preparing", progress=0.02)
        status, start = api.request(server, "POST", "/api/addon/upload", body, token=token)
        if status != 200:
            _job.update(error=start.get("error", "Couldn't start the upload."), code=start.get("code", ""), status=status)
            return
        _job.update(stage="Uploading", progress=0.05)
        status, put = api.put_file(start["signedUrl"], glb, lambda f: _job.update(progress=0.05 + 0.85 * f))
        if status not in (200, 201):
            _job.update(error=put.get("error", "The upload failed."), status=status)
            return
        _job.update(stage="Creating the review link", progress=0.93)
        status, done = api.request(server, "POST", "/api/addon/complete", {
            "assetId": start["assetId"], "path": start["path"], "newAsset": start["newAsset"],
            "name": body["name"], **({"projectId": start["projectId"]} if start.get("projectId") else {}),
        }, token=token, timeout=90)
        if status != 200:
            _job.update(error=done.get("error", "Couldn't finish the upload."), status=status)
            return
        _job.update(result=done, progress=1.0, stage="Done")
    except api.Offline:
        _job.update(error="Online access was turned off.")
    except Exception as e:  # anything unexpected becomes a message in the panel
        _job.update(error=f"Something went wrong ({e.__class__.__name__}).")
    finally:
        _job["finished"] = True
        try:
            os.remove(glb)
        except OSError:
            pass


class MESHMARKUP_OT_send(bpy.types.Operator):
    """Export the scene as GLB and send it to MeshMarkup. You get a review link to give your client"""
    bl_idname = "meshmarkup.send"
    bl_label = "Send for review"

    as_version: BoolProperty(name="As next version", default=False, options={"SKIP_SAVE"})
    _timer = None

    @classmethod
    def poll(cls, context):
        return not context.window_manager.meshmarkup_busy

    def execute(self, context):
        p = prefs(context)
        wm = context.window_manager
        scene = context.scene
        wm.meshmarkup_error = ""
        if not api.online():
            wm.meshmarkup_error = "Online access is off. Turn on Allow Online Access to send."
            return {"CANCELLED"}
        if not p.token:
            wm.meshmarkup_error = "Connect to MeshMarkup first."
            return {"CANCELLED"}
        if scene.meshmarkup_selection and not context.selected_objects:
            wm.meshmarkup_error = "Nothing is selected. Select objects, or untick Selection only."
            return {"CANCELLED"}

        wm.meshmarkup_busy = True
        wm.meshmarkup_stage = "Exporting"
        wm.meshmarkup_progress = 0.0
        redraw()
        name = (scene.meshmarkup_name or blend_name()).strip()[:120] or "Untitled"
        glb = os.path.join(export_dir(), f"send_{int(time.time())}.glb")
        try:
            size = export_glb(context, glb, scene.meshmarkup_selection)
        except Exception as e:
            wm.meshmarkup_busy = False
            wm.meshmarkup_error = f"The GLB export failed: {e}"
            return {"CANCELLED"}

        body = {"name": name, "bytes": size}
        if self.as_version and scene.meshmarkup_asset_id:
            body["assetId"] = scene.meshmarkup_asset_id
        elif scene.meshmarkup_project and scene.meshmarkup_project != "DEFAULT":
            body["projectId"] = scene.meshmarkup_project

        _job.clear()
        _job.update(stage="Preparing", progress=0.0)
        threading.Thread(target=_send_worker, args=(p.server, p.token, body, glb), daemon=True).start()
        self._timer = wm.event_timer_add(0.2, window=context.window)
        wm.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def modal(self, context, event):
        if event.type != "TIMER":
            return {"PASS_THROUGH"}
        wm = context.window_manager
        wm.meshmarkup_stage = _job.get("stage", "")
        wm.meshmarkup_progress = _job.get("progress", 0.0)
        redraw()
        if not _job.get("finished"):
            return {"PASS_THROUGH"}

        wm.event_timer_remove(self._timer)
        wm.meshmarkup_busy = False
        if _job.get("error"):
            if _job.get("status") == 401:
                prefs(context).token = ""
            if _job.get("code") in ("asset_gone",):
                forget_link(context.scene)
            wm.meshmarkup_error = _job["error"]
            self.report({"ERROR"}, _job["error"])
            return {"CANCELLED"}

        r = _job["result"]
        s = context.scene
        s.meshmarkup_asset_id = r["assetId"]
        s.meshmarkup_asset_name = r.get("name", "")
        s.meshmarkup_version = int(r.get("version", 1))
        s.meshmarkup_review_url = r.get("reviewUrl") or ""
        s.meshmarkup_model_url = r.get("modelUrl", "")
        s.meshmarkup_carried = int(r.get("carried", 0))
        if s.meshmarkup_review_url:
            wm.clipboard = s.meshmarkup_review_url
        remember({"asset": r["assetId"], "name": s.meshmarkup_asset_name, "version": s.meshmarkup_version,
                  "review": s.meshmarkup_review_url, "model": s.meshmarkup_model_url})
        self.report({"INFO"}, f"Sent {s.meshmarkup_asset_name} v{s.meshmarkup_version}. Review link copied")
        redraw()
        return {"FINISHED"}


def forget_link(scene):
    scene.meshmarkup_asset_id = scene.meshmarkup_asset_name = ""
    scene.meshmarkup_review_url = scene.meshmarkup_model_url = ""
    scene.meshmarkup_version = scene.meshmarkup_carried = 0


class MESHMARKUP_OT_unlink(bpy.types.Operator):
    """Stop treating this file as a version of that model. The next send makes a new model"""
    bl_idname = "meshmarkup.unlink"
    bl_label = "Send as a new model next time"

    def execute(self, context):
        forget_link(context.scene)
        return {"FINISHED"}


class MESHMARKUP_OT_copy(bpy.types.Operator):
    """Copy the link to the clipboard"""
    bl_idname = "meshmarkup.copy"
    bl_label = "Copy link"

    url: StringProperty(options={"SKIP_SAVE"})

    def execute(self, context):
        context.window_manager.clipboard = self.url
        self.report({"INFO"}, "Link copied")
        return {"FINISHED"}


class MESHMARKUP_OT_dismiss(bpy.types.Operator):
    """Hide this message"""
    bl_idname = "meshmarkup.dismiss"
    bl_label = "Dismiss"

    def execute(self, context):
        context.window_manager.meshmarkup_error = ""
        return {"FINISHED"}


# -------------------------------------------------------------------- UI
def draw_intro(layout, context):
    """What this is, before anything is connected."""
    width = context.region.width if context.region else 300
    box = layout.box()
    wrap(box, "Send this scene to your client as one link. They open it in a browser and click the model to "
              "leave notes, pinned exactly where they mean.", width - 30, "WORLD")
    col = layout.column()
    wrap(col, "Needs a free MeshMarkup account. You can create one while connecting.", width - 20, "INFO")
    col.separator()
    big = col.column()
    big.scale_y = 1.5
    big.operator("meshmarkup.connect", text="Create free account & connect", icon="ADD")
    col.operator("meshmarkup.connect", text="I have an account — connect", icon="URL")


def draw_offline(layout, context):
    box = layout.box()
    box.alert = True
    wrap(box, "Online access is off, so MeshMarkup can't send anything.", (context.region.width if context.region else 300) - 30, "ERROR")
    if bpy.app.online_access_override:
        box.label(text="Blender was started in offline mode.")
    else:
        box.prop(context.preferences.system, "use_online_access", text="Allow Online Access")


class MESHMARKUP_PT_panel(bpy.types.Panel):
    bl_label = "MeshMarkup"
    bl_idname = "MESHMARKUP_PT_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "MeshMarkup"

    def draw_header(self, context):
        self.layout.label(text="", icon_value=icon())

    def draw(self, context):
        layout = self.layout
        p = prefs(context)
        wm = context.window_manager
        scene = context.scene
        width = context.region.width

        if wm.meshmarkup_error:
            box = layout.box()
            row = box.row()
            wrap(row, wm.meshmarkup_error, width - 60, "ERROR", alert=True)
            row.operator("meshmarkup.dismiss", text="", icon="X", emboss=False)

        if not api.online():
            draw_offline(layout, context)

        if wm.meshmarkup_code:
            box = layout.box()
            box.label(text="Approve this code in your browser", icon="KEYINGSET")
            code = box.row()
            code.scale_y = 2.2
            code.alignment = "CENTER"
            code.label(text=wm.meshmarkup_code)
            box.label(text="Waiting for approval…", icon="SORTTIME")
            box.operator("meshmarkup.cancel_connect", icon="X")
            return

        if not p.token:
            draw_intro(layout, context)
            return

        # ---- account
        row = layout.row(align=True)
        row.label(text=p.studio or "Connected", icon="CHECKMARK")
        if p.plan:
            sub = row.row()
            sub.enabled = False
            sub.label(text=p.plan)
        row.operator("meshmarkup.refresh", text="", icon="FILE_REFRESH")
        row.operator("meshmarkup.disconnect", text="", icon="UNLINKED")

        # ---- send
        box = layout.box()
        col = box.column(align=True)
        col.prop(scene, "meshmarkup_name", text="", placeholder=blend_name(), icon="OBJECT_DATA")
        if len(projects_items(None, context)) > 1 and not scene.meshmarkup_asset_id:
            col.prop(scene, "meshmarkup_project", text="", icon="FILE_FOLDER")
        sel = box.row()
        sel.prop(scene, "meshmarkup_selection")
        if scene.meshmarkup_selection:
            n = len(context.selected_objects)
            sub = sel.row()
            sub.enabled = False
            sub.label(text=f"{n} selected")

        if wm.meshmarkup_busy:
            box.separator()
            try:
                box.progress(factor=wm.meshmarkup_progress, type="BAR",
                             text=f"{wm.meshmarkup_stage} {int(wm.meshmarkup_progress * 100)}%"
                             if wm.meshmarkup_stage == "Uploading" else f"{wm.meshmarkup_stage}…")
            except AttributeError:   # Blender without UILayout.progress
                box.label(text=f"{wm.meshmarkup_stage}… {int(wm.meshmarkup_progress * 100)}%", icon="SORTTIME")
        elif scene.meshmarkup_asset_id:
            link = box.row(align=True)
            link.label(text=f"{scene.meshmarkup_asset_name} · v{scene.meshmarkup_version}", icon="LINKED")
            link.operator("meshmarkup.unlink", text="", icon="X", emboss=False)
            big = box.column()
            big.scale_y = 1.6
            op = big.operator("meshmarkup.send", text=f"Upload as v{scene.meshmarkup_version + 1}", icon_value=icon())
            op.as_version = True
            op = box.operator("meshmarkup.send", text="Send as a new model", icon="ADD")
            op.as_version = False
        else:
            big = box.column()
            big.scale_y = 1.6
            op = big.operator("meshmarkup.send", text="Send for review", icon_value=icon())
            op.as_version = False

        # ---- the link from the last send
        if scene.meshmarkup_review_url and not wm.meshmarkup_busy:
            done = layout.box()
            done.label(text="Review link for your client", icon="CHECKMARK")
            url = done.row()
            url.enabled = False
            url.label(text=scene.meshmarkup_review_url.replace("https://", ""))
            if scene.meshmarkup_carried:
                done.label(text=f"{scene.meshmarkup_carried} open note{'s' if scene.meshmarkup_carried != 1 else ''} carried over", icon="PINNED")
            row = done.row(align=True)
            row.operator("meshmarkup.copy", text="Copy", icon="COPYDOWN").url = scene.meshmarkup_review_url
            row.operator("wm.url_open", text="Open as client", icon="URL").url = scene.meshmarkup_review_url
            if scene.meshmarkup_model_url:
                done.operator("wm.url_open", text="See notes on MeshMarkup", icon="WINDOW").url = scene.meshmarkup_model_url


class MESHMARKUP_PT_recent(bpy.types.Panel):
    bl_label = "Recent sends"
    bl_idname = "MESHMARKUP_PT_recent"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "MeshMarkup"
    bl_parent_id = "MESHMARKUP_PT_panel"
    bl_options = {"DEFAULT_CLOSED"}

    @classmethod
    def poll(cls, context):
        return bool(prefs(context).token)

    def draw(self, context):
        try:
            recent = json.loads(prefs(context).recent or "[]")
        except Exception:
            recent = []
        if not recent:
            self.layout.label(text="Nothing sent yet")
            return
        col = self.layout.column(align=True)
        for r in recent:
            row = col.row(align=True)
            row.label(text=f"{r.get('name', '')}  v{r.get('version', '')}", icon="FILE_3D")
            if r.get("review"):
                row.operator("meshmarkup.copy", text="", icon="COPYDOWN").url = r["review"]
            if r.get("model"):
                row.operator("wm.url_open", text="", icon="URL").url = r["model"]


class MESHMARKUP_PT_export(bpy.types.Panel):
    bl_label = "Export only"
    bl_idname = "MESHMARKUP_PT_export"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "MeshMarkup"
    bl_parent_id = "MESHMARKUP_PT_panel"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        col = self.layout.column()
        wrap(col, "A GLB ready for review, saved where you choose. Works offline.", context.region.width - 20)
        col.operator("meshmarkup.export_glb", icon="EXPORT")


def menu_export(self, context):
    op = self.layout.operator("meshmarkup.send", text="MeshMarkup review link", icon_value=icon())
    op.as_version = bool(context.scene.meshmarkup_asset_id)


CLASSES = (
    MESHMARKUP_Preferences, MESHMARKUP_OT_connect, MESHMARKUP_OT_cancel_connect, MESHMARKUP_OT_refresh,
    MESHMARKUP_OT_disconnect, MESHMARKUP_OT_export_glb, MESHMARKUP_OT_send, MESHMARKUP_OT_unlink,
    MESHMARKUP_OT_copy, MESHMARKUP_OT_dismiss, MESHMARKUP_PT_panel, MESHMARKUP_PT_recent, MESHMARKUP_PT_export,
)


def register():
    global _icons
    _icons = bpy.utils.previews.new()
    _icons.load("meshmarkup", os.path.join(os.path.dirname(__file__), "icons", "meshmarkup.png"), "IMAGE")
    for c in CLASSES:
        bpy.utils.register_class(c)
    register_props()
    bpy.types.TOPBAR_MT_file_export.append(menu_export)


def unregister():
    global _icons
    bpy.types.TOPBAR_MT_file_export.remove(menu_export)
    unregister_props()
    for c in reversed(CLASSES):
        bpy.utils.unregister_class(c)
    if _icons:
        bpy.utils.previews.remove(_icons)
        _icons = None
