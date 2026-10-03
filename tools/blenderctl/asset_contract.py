# SPDX-License-Identifier: GPL-3.0-or-later
"""Strict asset-candidate manifests. No arbitrary code or implicit source overwrite."""
from pathlib import Path
import uuid
import re
from protocol import Failure, SCHEMA
from verification_contract import fields, require


def normalize(manifest, command, file):
    from protocol import validate
    if command == "asset.prepare":
        fields(manifest, ("operation","targets","keep_dependency_assets","allow_version_conversion"), ("operation","targets"))
        require(manifest["operation"] in {"extract","edit","unmark","quarantine","restore","remove"}, "Unknown asset operation")
        require(type(manifest.get("keep_dependency_assets",False)) is bool, "keep_dependency_assets must be boolean")
        require(manifest["operation"] == "extract" or "keep_dependency_assets" not in manifest, "Dependency mark policy applies only to extraction")
        require(type(manifest.get('allow_version_conversion',False)) is bool, 'Version conversion policy must be boolean')
        require(manifest['operation']=='extract' or 'allow_version_conversion' not in manifest, 'Version conversion only applies to extracted candidates')
        require(isinstance(manifest["targets"],list) and 1 <= len(manifest["targets"]) <= 100, "Requires 1..100 targets")
        for t in manifest["targets"]:
            fields(t, ("selector","name","description","author","tags","catalog_id","preview"), ("selector",))
            t["selector"] = validate({"schema_version":SCHEMA,"command":"query","params":{"file":file,"selector":t["selector"]}})["params"]["selector"]
            require(manifest["operation"] in {"extract","edit"} or set(t)=={"selector"}, "Metadata is accepted only by extract/edit")
            for k in ("name","description","author","catalog_id"):
                if k in t:
                    require(isinstance(t[k],str) and "\0" not in t[k] and len(t[k]) <= 16384, "Invalid metadata string")
            if "name" in t:
                require(bool(t["name"].strip()), "Name must not be empty")
            if "catalog_id" in t:
                try:
                    require(str(uuid.UUID(t["catalog_id"]))==t["catalog_id"].lower(), "Requires canonical Catalog UUID")
                    t["catalog_id"]=t["catalog_id"].lower()
                except ValueError:
                    raise Failure("INVALID_REQUEST","Invalid Catalog UUID")
            if "tags" in t:
                require(isinstance(t["tags"],list) and len(t["tags"])<=100 and all(isinstance(v,str) and 0<len(v)<=63 and "\0" not in v for v in t["tags"]), "Invalid tags")
                require(len(set(t["tags"]))==len(t["tags"]), "Duplicate tags")
            if "preview" in t:
                fields(t['preview'], ('file','expected_sha256'), ('file','expected_sha256'))
                p = t['preview']
                require(isinstance(p['file'],str) and '\0' not in p['file'] and Path(p['file']).is_absolute(), 'Preview requires an absolute file path')
                require(Path(p['file']).is_file() and Path(p['file']).suffix.lower() in {'.png','.jpg','.jpeg'}, 'Preview must be an existing PNG or JPEG')
                require(isinstance(p['expected_sha256'],str) and bool(re.fullmatch('[0-9a-f]{64}',p['expected_sha256'])), 'Preview requires SHA256')
                p['file'] = str(Path(p['file']).resolve())
    else:
        fields(manifest, ("output_file","library_root","assets","catalogs"), ("output_file","library_root","assets","catalogs"))
        for k in ("output_file","library_root"):
            require(isinstance(manifest[k],str) and "\0" not in manifest[k] and Path(manifest[k]).is_absolute(), "Requires absolute output/library path")
            manifest[k]=str(Path(manifest[k]).resolve())
        require(Path(manifest["library_root"]).is_dir(), "Library root must exist")
        require(Path(manifest["output_file"]).suffix.lower()==".blend" and Path(manifest["output_file"]).is_relative_to(Path(manifest["library_root"])), "Output blend must stay in library root")
        require(isinstance(manifest["assets"],list) and len(manifest["assets"])<=1000, "Invalid asset index descriptors")
        for spec in manifest["assets"]:
            fields(spec,("asset_id","type","category","version","source"),("asset_id","type","category","version","source"))
            require(all(isinstance(spec[k],str) for k in ("asset_id","type","category","version")) and isinstance(spec["source"],dict), "Invalid asset index descriptor")
        require(isinstance(manifest["catalogs"],list) and len(manifest["catalogs"])<=1000, "Invalid Catalog entries")
        ids=set()
        for c in manifest["catalogs"]:
            fields(c,("uuid","path","simple_name"),("uuid","path","simple_name"))
            require(all(isinstance(c[k],str) and c[k] and not any(ch in c[k] for ch in ("\n","\r",":","\0")) for k in c), "Invalid Catalog field")
            try:
                uid=str(uuid.UUID(c["uuid"]))
            except ValueError:
                raise Failure("INVALID_REQUEST","Invalid Catalog UUID")
            require(uid==c["uuid"].lower() and uid not in ids, "Noncanonical or duplicate Catalog UUID")
            c["uuid"]=uid
            ids.add(uid)
    return manifest
