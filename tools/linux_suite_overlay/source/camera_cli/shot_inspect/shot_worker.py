import bpy, sys, json
sys.path.insert(0,"/trusted")
import pinned_schema, shot_table
assert bpy.app.background and bpy.app.version==(5,3,0) and bpy.app.build_hash==b"02f0899d78e3"
q=json.load(open("/query.json"))
pinned_schema.register_for_read(bpy)
assert bpy.ops.wm.open_mainfile(filepath="/input.blend",load_ui=False,use_scripts=False)=={"FINISHED"}
assert not len(bpy.data.libraries) and not len(bpy.data.texts),"linked or scripted input refused"
assert len(bpy.data.scenes)<=16 and len(bpy.data.objects)<=2048,"scene budget"
s=bpy.data.scenes.get(q["scene"]) if q.get("scene") else bpy.context.scene
assert s is not None,"scene not present"
before=(s.frame_current,s.frame_subframe,s.camera,bpy.data.is_dirty)
out=shot_table.inspect(s,q["frame"])
assert before==(s.frame_current,s.frame_subframe,s.camera,bpy.data.is_dirty)
print("SHOT-INSPECT:"+json.dumps(out,ensure_ascii=True,allow_nan=False,separators=(",",":")))
