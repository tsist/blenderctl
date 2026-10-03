#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# Explicit BLENDERCTL_PYTHON wins; otherwise use the host's python3.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec "${BLENDERCTL_PYTHON:-python3}" "$ROOT/tools/blenderctl/cli.py" "$@"
