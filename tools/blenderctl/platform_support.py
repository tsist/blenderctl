# SPDX-License-Identifier: GPL-3.0-or-later
"""Expose platform boundaries without claiming registration is validation."""
import sys


def capabilities():
    if sys.platform == 'linux':
        return {
            'platform': 'linux',
            'read_guard': 'LINUX_LEASE_INOTIFY_V1',
            'read_guard_requirements': [
                'Qualified local filesystem and namespace; leases and inotify must work',
                'Normalized real targets; guard traversal forbids symlinks; no existing writable handle/mapping',
                'Lease break, namespace mutation, watch overflow or lost evidence rejects the result',
            ],
            'unsupported': [
                'Windows-style atomic file transactions and PathLocks',
                'Pipeline orchestration requiring PathLocks and hard Windows Job memory limits',
                'Windows AppContainer script execution',
                'Overlay/network filesystems as protected input roots',
            ],
            'scope': 'Protected static saved-file workflows; not a malicious-user/kernel sandbox. '
                     'Adapter-specific Blender version checks remain authoritative.',
        }
    return {'platform': sys.platform,
            'read_guard': 'WINDOWS_DENY_WRITE_DELETE' if sys.platform == 'win32' else 'UNSUPPORTED',
            'scope': 'Adapter-specific operating-system and Blender version checks remain authoritative.'}
