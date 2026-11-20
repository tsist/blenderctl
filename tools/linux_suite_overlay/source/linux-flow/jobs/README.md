# Bounded one-shot Linux Blender jobs

Installed public routing is maintained separately. Standalone syntax:

    jobs.py --root /absolute/private-0700-root submit --domain scene --manifest /absolute/scene.json --source /absolute/working-source.blend --sha <64-lowercase-hex> --timeout 120
    jobs.py --root /absolute/private-0700-root status <job_id>
    jobs.py --root /absolute/private-0700-root cancel <job_id>
    jobs.py --root /absolute/private-0700-root result <job_id>

Only named scene/model prepare operations. Both source flags may be omitted together (factory startup). Source size ≤512 MiB; manifest ≤1 MiB and 1–128 operations; worker timeout 1–300 s followed by guarded cleanup, 2 concurrent jobs and 256 retained IDs per root. Source and manifest paths must be absolute and symlink-free. Root must exist and belong to the invoking uid, mode 0700. Pin the previously qualified launcher as suite/bin/blenderctl-prepare-v1 before replacing the public dispatcher. Runtime is the qualified fixed-location Linux Blender 5.3.0.

Submission returns an ID, not a result. A terminal succeeded result binds input, manifest, named argv, runtime hashes and checked reopened candidate; result/status recheck candidate and backend evidence. Source numerical PID is never used for cancellation by an external client. A cooperative cancel token, Linux inherited file lock, parent-death signal and atomic fsynced receipts allow independent clients to observe the same private job without exposing an execution service. Filesystem must support local kernel file locks. Do not move/remove backend job files if durable result revalidation is needed. SHA-256 guards against accidental mismatches; neither hashes nor 0700 protect against a malicious owner rewriting all trusted code and receipts.

Terminal states: succeeded, failed, timed_out, cancelled, orphaned. A caller receiving a terminal JSON receipt must inspect state; exit 0 means the receipt was readable, not necessarily that the job succeeded. If the original worker dies, missing lease makes the job orphaned; partial files are not accepted or promoted, no automatic replay. New attempts get new IDs. No daemon, queue service, shell injection interface or startup persistence. On hosts that destroy detached processes when an execution namespace closes, keep the originating execution session alive until receipt creation. Run ordinary submit + polling from one persistent shell or maintain a live execution-tool session. A different process namespace can still query status and request cancellation via the common private filesystem.

The original qualification harness was not included in the recovered overlay. An installed copy needs a real scene prepare/reopen and cancellation test before use.
