# Third-party dependencies and attribution

This repository's CLI, material extension, schemas, examples and release tooling are distributed under **GPL-3.0-or-later**. See LICENSE. Copyright © 2026 blenderctl contributors. The extension's existing SPDX declaration is retained. Distribution archives contain this project's source, not a bundled Blender installation or asset library.

External runtimes and optional facilities:

| Dependency | Purpose | Distributed here | License / source |
| --- | --- | --- | --- |
| Python | Host CLI and release/test tooling | No | Python Software Foundation license; python.org |
| Blender (`bpy`, `bmesh`, `mathutils`, `bpy_extras`) | Background workers and extension | No | Blender GPL license and component notices; blender.org |
| Blender glTF / FBX import-export modules | Exchange adapters; imported from installed Blender | No | Consult the exact Blender distribution's component licenses |
| NumPy | Numerical geometry/image work in Blender | No | BSD-3-Clause; numpy.org |
| OpenUSD / `pxr` | Selected mesh/time exchange paths | No | Apache-2.0; openusd.org |

The scoped FBX compatibility adapter temporarily patches a class imported from Blender's installed FBX module and restores it; it does not ship a replacement Blender exporter/importer or modify installed files. Native adapters retain explicit version restrictions.

Release host tests use the Python standard library. Individual native adapters may require modules present in the selected Blender distribution; check the actual installation rather than installing substitutes automatically. GPU backends, codecs and OS APIs are provided by the user's environment under their own terms.

Source review for this export found no vendored third-party library implementation. This is a finite source review, not a legal determination about every future contribution. Contributors must identify copied code and its license before inclusion. User textures, node groups, fonts, media and assets are separate inputs; users remain responsible for their rights and distribution permissions. Examples and integration fixtures generate simple geometry and numerical textures locally.
