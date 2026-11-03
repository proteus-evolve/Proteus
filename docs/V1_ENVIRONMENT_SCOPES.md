# Filesystem scopes and process limits in Harbor code environments

`proteus.harbor_code@1` accepts container directory trees in
`EnvironmentSpec.read_scopes` and `write_scopes`, and an optional `resources.pids`
limit. The independent and explicit-memory sequence modes use the same policy.
Other providers continue to reject declarations they cannot enforce. This is a
backend capability, not an assumption that every environment has path isolation.

For the supplied Python code image, a scoped configuration can declare:

```json
{
  "resources": {"cpus": 1, "memory_mb": 512, "pids": 32},
  "read_scopes": ["/usr", "/opt/proteus"],
  "write_scopes": ["/opt/proteus/candidate"]
}
```

These fields belong inside the existing `execution.environment` object. The
normal immutable image, Harbor runtime, no-network policy and execution timeout
are still required. With a source build, the executable tree is
`/opt/proteus/build`; declare that directory if the candidate needs to write there.
The source witness remains `/opt/proteus/source`. Scopes grant no host mounts,
controller data or evaluator files. Only the existing explicit code protocol
crosses to the host actor service.

These scopes constrain the code-protocol invocation. Earlier source-build
commands use the separate build policy and are not restricted by this Landlock
ruleset; declaring scopes does not extend them to the build phase.

## Meaning of a scope

Each scope names an existing absolute container directory. Relative paths,
traversal, noncanonical spellings, duplicates and oversized declarations are
rejected. Rules apply to the filesystem objects resolved at bootstrap, including
trusted image symlinks. A recipe must include the interpreter, runtime libraries
and candidate/helper directories it needs to read. A missing or insufficient
scope is an error; Proteus does not add an implicit allowlist or retry without it.

Read scopes allow file reads, directory listing and program execution. Write
scopes allow file content writes, creation, removal, rename/link and truncation
within those trees, subject also to normal Unix permissions and Docker policy.
Read and write grants are independent. An empty write list denies these writes
when read scopes are declared. Both lists empty preserve the previous container
policy and do not claim path restrictions. A write list without a read list is
rejected because this backend needs an explicitly readable runtime hierarchy.

The restriction uses [Linux Landlock](https://docs.kernel.org/userspace-api/landlock.html)
ABI 3 or newer on x86_64/aarch64. It applies before candidate code, threads or
child processes start, and descendants inherit it. The bootstrap refuses an
unsupported kernel, disabled Landlock or a denied syscall. It never drops a
requested right from the policy to accommodate an older kernel.

This policy covers the ABI 3 filesystem access operations. File metadata
operations such as `stat`/`chmod`, device ioctls, network and already-open protocol
descriptors are separate controls. It does not claim a complete operating-system
sandbox. Existing zero host mounts, dropped Linux capabilities, unprivileged
candidate user, no-new-privileges and no-network namespace remain in force.

`resources.pids` must be an integer of at least 8. When supplied it selects the
Docker process limit; otherwise the existing backend option `max_pids` remains
the fallback, defaulting to 64. CPU and memory remain mandatory. Other resource
keys fail validation. Independent Harbor task trials and persistent research
workers retain their separately declared backend capabilities.

## Dispatch evidence and recovery

The private request binds the exact scope policy and standalone bootstrap bytes.
After normal candidate/image/resource inspection, the worker launches the
bootstrap as UID/GID 65534. It applies the rules in a single thread, emits a bounded
receipt and execs the ordinary code-protocol child. The owner validates that
receipt before forwarding any task input or managed model request. The receipt
records the policy, actual Landlock ABI, handled rights and user identity.

`code-private/loaded.json` binds this receipt and bootstrap hash to the candidate,
request and container witness. The process limit is independently inspected from
Docker. A failed bootstrap cannot produce a successful loaded-policy witness.
These records are owner-controlled evidence, not signatures against an owner
able to rewrite all controller records.

Normal sequence reset, source builds, deadlines, cleanup and settled-result
recovery keep the existing semantics. A completed resume does not recreate the
container or add another policy attempt. Unknown external execution still needs
reconciliation; policy enforcement does not itself prove the task completed.

## Acceptance

Linux tests execute the actual bootstrap and check permitted file operations,
read-only trees, denied reads/writes/truncation, symlink escape denial, and
inherited restrictions in a subprocess.
Installed wheel and sdist checks require this kernel test on Linux CI. Unsupported
platform checks verify refusal before application startup.

The mandatory Harbor gate additionally runs the code-memory recipe through six
real containers with scoped reads/writes and a 32-process limit. It checks
container-only filesystem access, child inheritance, sequence reset, unchanged
resume and request/receipt/image/cleanup bindings. A local host without Landlock
can validate explicit refusal and configuration behavior; it cannot substitute
for this positive Linux/Docker gate. Real model experiments remain separate.
