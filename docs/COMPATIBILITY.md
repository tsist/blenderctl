# Compatibility and support boundaries

For the Linux branch (CLI 0.55.0 / extension 0.7.1), see [Linux support](LINUX.md).
Protected saved-file workflows have a bounded Linux lease/inotify contract. Windows
transactions, pipeline locks/hard memory limits and AppContainer remain unsupported;
the original exact Blender-version restrictions below are preserved.

The verified native baseline is **Windows / Blender 5.2.1 LTS / build 9e2066aef7ef**. This release uses host Python 3.12+; local integration runs use Blender's Python 3.13.13. Source distribution does not install or upgrade the user's runtime.

Several native drivers, links/overrides, retopology and format-time adapters explicitly restrict Blender versions. The extension minimum of Blender 5.2.0 is an installation declaration, not a statement that every Blender 5.2 build was exercised. Check the actual adapter and runtime result.

Windows Job Objects, shared-delete file reads and AppContainer APIs have Windows semantics. POSIX process code exists, but Linux/macOS full runtime behavior is not qualified by the Windows integration run. GitHub Linux jobs exercise host contracts and packaging only.

Cycles/Eevee render paths and static managed PBR inputs have bounded node/engine contracts. GPU enumeration is not proof of successful GPU rendering. The public release smoke test uses CPU; no new GPU, Eevee, animation, batch recovery, GUI interaction, artistic or physical material certification is claimed.

Working candidates, dependency SHA verification, atomic JSON records and guarded transactions do not guarantee simultaneous multi-file visibility, power-loss consistency, hard disk/VRAM quotas or safety of arbitrary trusted Python. No cloud service or network share compatibility is implied.

Private historical validation informed the adapters' original restrictions. Its audit directories and user assets are not distributed. Public validation is independently reproducible with generated fixtures; see [release verification](RELEASE_VERIFICATION.md).
