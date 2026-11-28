"""Content identities for the managed runtime, excluding mutable user configuration."""
import os
import stat
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from contextlib import nullcontext
from pathlib import Path
from threading import Event, Lock

from filesystem import FileGuard, native_path
from protocol import Failure


def included_runtime_dir(name):
    return name[:1].isdigit() or name in ('lib', 'blender.shared', 'blender.crt')


def skipped_runtime_entry(name):
    return name == '__pycache__' or name.endswith('.pyc')


def _reparse(info):
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )


def _inventory_root(blender):
    # Check each ancestor once, without resolving away a junction before checking it.
    root = Path(native_path(Path(blender).absolute().parent))
    for directory in (*reversed(root.parents), root):
        if _reparse(directory.lstat()):
            raise Failure('UNSUPPORTED', f'Runtime inventory cannot follow links: {directory}')
    return root


def _scan(root, checkpoint, stack=None):
    files = []

    def visit(directory, distribution_root=False):
        checkpoint()
        if stack is not None:
            stack.enter_context(FileGuard(directory, directory=True))
        with os.scandir(directory) as entries:
            for entry in entries:
                if not distribution_root and skipped_runtime_entry(entry.name):
                    continue
                info = entry.stat(follow_symlinks=False)
                is_directory = stat.S_ISDIR(info.st_mode)
                in_scope = not distribution_root or included_runtime_dir(entry.name)
                if _reparse(info):
                    # Portable preferences and unrelated top-level directories are excluded.
                    has_directory_attribute = bool(
                        getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_DIRECTORY
                    )
                    if not in_scope and (is_directory or has_directory_attribute):
                        continue
                    raise Failure('UNSUPPORTED', f'Runtime inventory cannot follow links: {entry.path}')
                if is_directory:
                    if in_scope:
                        visit(entry.path)
                elif stat.S_ISREG(info.st_mode):
                    files.append(Path(entry.path))

    visit(root, distribution_root=True)
    return sorted(files)


def runtime_files(blender):
    return _scan(_inventory_root(blender), lambda: None)


def verify_members(blender, inventory, checkpoint):
    root = _inventory_root(blender)
    actual = {str(path.relative_to(root)) for path in _scan(root, checkpoint)}
    if actual != set(inventory):
        raise Failure('CONFLICT', 'Runtime file membership changed during execution')


def _parallel_inventory(files, root, stack, checkpoint):
    """Overlap Windows opens and reads, keeping every successful guard alive."""
    registrations = Lock()
    stopped = Event()

    def check_stop():
        if stopped.is_set():
            raise Failure('CANCELLED', 'Runtime inventory stopped')

    def read(path):
        check_stop()
        guard = FileGuard(path)
        with registrations:
            stack.enter_context(guard)
        return str(path.relative_to(root)), {'sha256': guard.sha256(check_stop), 'bytes': guard.size}

    pool = ThreadPoolExecutor(max_workers=4)
    try:
        pending = [pool.submit(read, path) for path in files]
        rows = {}
        for future in pending:
            while True:
                checkpoint()
                try:
                    path, row = future.result(timeout=.05)
                except TimeoutError:
                    if future.done():
                        # A completed read can itself report a timeout.
                        future.result()
                    continue
                rows[path] = row
                break
        return rows
    finally:
        stopped.set()
        pool.shutdown(wait=True, cancel_futures=True)


def content_inventory(blender, checkpoint, *, stack=None):
    """Hash exact bytes. Pipelines keep read guards alive throughout execution."""
    root = _inventory_root(blender)
    if stack is not None:
        for directory in reversed(root.parents):
            stack.enter_context(FileGuard(directory, directory=True))
    files = _scan(root, checkpoint, stack)
    if stack is not None and len(files) >= 128:
        rows = _parallel_inventory(files, root, stack, checkpoint)
    else:
        rows = {}
        for path in files:
            checkpoint()
            guard = FileGuard(path)
            with guard if stack is None else nullcontext(stack.enter_context(guard)) as opened:
                rows[str(path.relative_to(root))] = {'sha256': opened.sha256(checkpoint), 'bytes': opened.size}
    # Exclusion of WRITE/DELETE protects known members but cannot keep a new
    # file from being created. Reject additions observed during the scan.
    if stack is not None and _scan(root, checkpoint) != files:
        raise Failure('CONFLICT', 'Runtime file membership changed during inventory')
    return rows
