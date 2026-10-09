# SPDX-License-Identifier: GPL-3.0-or-later
"""Talking to MeshMarkup. Every call checks Blender's "Allow Online Access" first."""
import json
import os
import urllib.error
import urllib.request
import uuid

import bpy

VERSION = "1.0.0"


class Offline(Exception):
    pass


def online():
    return bool(bpy.app.online_access)


def _ua():
    return f"MeshMarkup-Blender/{VERSION} Blender/{bpy.app.version_string}"


def request(server, method, path, body=None, token=None, timeout=30):
    """(status, json). Status 0 means the request never got an answer."""
    if not online():
        raise Offline()
    url = server.rstrip("/") + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("content-type", "application/json")
    req.add_header("user-agent", _ua())
    if token:
        req.add_header("authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except Exception:
            return e.code, {}
    except Exception as e:  # offline, DNS, TLS, timeout
        return 0, {"error": f"Couldn't reach MeshMarkup ({e.__class__.__name__})."}


def put_file(signed_url, path, on_progress, chunk=256 * 1024):
    """Send the GLB to storage as multipart form data (the way the web app does), reporting progress 0..1."""
    if not online():
        raise Offline()
    boundary = uuid.uuid4().hex
    head = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"cacheControl\"\r\n\r\n3600\r\n"
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"\"; filename=\"model.glb\"\r\n"
        f"Content-Type: model/gltf-binary\r\n\r\n"
    ).encode()
    tail = f"\r\n--{boundary}--\r\n".encode()
    size = os.path.getsize(path)
    total = len(head) + size + len(tail)

    def body():
        sent = 0
        yield head
        with open(path, "rb") as fh:
            while True:
                block = fh.read(chunk)
                if not block:
                    break
                sent += len(block)
                on_progress(sent / max(size, 1))
                yield block
        yield tail

    req = urllib.request.Request(signed_url, data=body(), method="PUT")
    req.add_header("content-type", f"multipart/form-data; boundary={boundary}")
    req.add_header("content-length", str(total))
    req.add_header("x-upsert", "true")
    req.add_header("user-agent", _ua())
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            return r.status, {}
    except urllib.error.HTTPError as e:
        return e.code, {"error": f"Storage refused the file ({e.code})."}
    except Exception as e:
        return 0, {"error": f"The upload was interrupted ({e.__class__.__name__})."}
