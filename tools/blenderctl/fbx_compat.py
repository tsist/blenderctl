# SPDX-License-Identifier: GPL-3.0-or-later
"""Scoped Blender 5.2.1 FBX importer traversal correction; no installed-file edits."""
from contextlib import contextmanager
import bpy


@contextmanager
def stable_armature_traversal():
    if tuple(bpy.app.version)!=(5,2,1):
        yield
        return
    from io_scene_fbx.import_fbx import FbxImportHelperNode
    original=FbxImportHelperNode.collect_armature_meshes
    def collect(node):
        if node.is_armature:
            return original(node)
        # Armature processing reparents skinned mesh siblings, mutating this
        # list. Iterating it live can skip the next armature entirely.
        for child in tuple(node.children):
            child.collect_armature_meshes()
    FbxImportHelperNode.collect_armature_meshes=collect
    try:yield
    finally:FbxImportHelperNode.collect_armature_meshes=original
