"""Minimal camera_live_shots schema bridge for an isolated, empty Blender worker BEFORE open.
Never installs an addon, persists preferences, adds a handler or registers a legacy operator.
Use only in a disposable background Blender process. Let process exit release type registration.
"""
def register_for_read(bpy):
    if not bpy.app.background or bpy.data.filepath:
        raise ValueError('empty background process required before opening source')
    if hasattr(bpy.types.Scene,'camera_live_shots') or hasattr(bpy.types,'CAMERA_LIVE_REBUILT_PG_shot'):
        raise ValueError('preexisting schema; refuse collision')
    p=bpy.props
    def camera_poll(self,obj):return obj.type=='CAMERA'
    class CAMERA_LIVE_REBUILT_PG_shot(bpy.types.PropertyGroup):
        name:p.StringProperty(name='Shot',default='Shot')
        camera:p.PointerProperty(name='Camera',type=bpy.types.Object,poll=camera_poll)
        first:p.IntProperty(name='Start',default=1,min=-1048574,max=1048574)
        last:p.IntProperty(name='End',default=24,min=-1048574,max=1048574)
    bpy.utils.register_class(CAMERA_LIVE_REBUILT_PG_shot)
    bpy.types.Scene.camera_live_shots=p.CollectionProperty(type=CAMERA_LIVE_REBUILT_PG_shot)

def reject_live_rows(scene):
    """Call immediately after open; never infer absence from .get or keys()."""
    prop=scene.bl_rna.properties.get('camera_live_shots')
    if not prop or prop.type!='COLLECTION' or prop.fixed_type.identifier!='CAMERA_LIVE_REBUILT_PG_shot':
        raise ValueError('expected pinned legacy shot schema unavailable')
    if len(scene.camera_live_shots):
        raise ValueError('saved legacy Live shot table is nonempty; do not edit, resave or publish')
    if 'camera_live_shots' in scene.keys():
        raise ValueError('additional legacy raw IDProperty storage; cannot prove empty')
