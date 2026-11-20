# Linux material template export smoke

Run this against a fresh checkout and official Blender 5.3.0 Linux x64:

    sh tests/linux_template_save/run.sh "$PWD" /absolute/path/to/blender

It creates a trusted synthetic managed material in a new private temporary folder.
The actual filesystem must qualify for source read leases and inotify; a path
name alone cannot qualify it. Overlay and unsupported filesystems fail closed.
No renderer or preview is invoked. The fixture uses preview metadata required
by the upstream material schema; only the material graph is constructed.
Review the issued result before using any job content. Copy accepted outputs
to persistent storage before discarding temporary files.
