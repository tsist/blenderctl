# Security policy

The latest public release receives security fixes as maintainer capacity permits. There is no guaranteed response time or long-term support commitment.

Report a suspected vulnerability through GitHub's **Report a vulnerability** entry on the repository Security tab. If private reporting is unavailable, open a minimal issue asking for a private channel without publishing an exploit, credential or private file. Ordinary bugs belong in public issues with sanitized reproductions.

The CLI starts Blender and, for explicitly trusted script operations, can execute Python with the current user's permissions. The experimental Windows AppContainer route has a distinct acknowledgement and no silent fallback. SHA checks establish content identity, not safety or licensing. Review unknown `.blend` files, manifests, scripts and dependencies before running them. Blender extension filesystem permissions cover reading textures/manifests and writing persistent snapshots/receipts.

Use separate output directories and preserve required job artifacts. Cancellation is limited to owned process trees. File transactions, publication and rollback are explicit operations; do not treat the presence of an output file as a successful terminal job result.
