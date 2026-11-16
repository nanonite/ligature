# Tested verifier compatibility and reproducible toolchains — v1.0 (#174)

Ligature publishes the verifier/toolchain combinations it was actually
exercised against. This is a **measured compatibility record**, not an
upstream support guarantee and not approval of production proof coverage.

The authoritative, machine-readable document is
[`docs/verifier-compatibility-matrix.json`](verifier-compatibility-matrix.json),
validated by
[`docs/verifier-compatibility-matrix-schema.json`](verifier-compatibility-matrix-schema.json).
Run any of these read-only surfaces:

```sh
ligature version                 # summary plus every tested entry
ligature doctor                  # same matrix, plus installation checks
ligature toolchain list --verbose
ligature toolchain show <entry-id>
ligature toolchain plan --entry <entry-id> --json
ligature toolchain lock --entry <entry-id>
```

## What the matrix records

Each entry fixes:

- the verifier and its exact version;
- the Rust toolchain (and, where recorded, the `rustc` version/commit);
- solver/support tools, including the Creusot `creusot-std` pair member;
- the platform (`os`, `architecture`, target triple);
- the exact Ligature build(s) the measurement was made with (artifact
  SHA-256, content hash and source commit);
- retained evidence paths with SHA-256 hashes;
- the result class of every evidence item: `intake-only`, `synthetic`, or
  `project-level`;
- the source identity and, where available, a lockable Nix source/input
  identity;
- the entry's exclusions.

`intake-only` means the tool launched and its output/environment could be
recorded. `synthetic` means a purpose-built probe obligation was proved or
checked. `project-level` means a declared project source obligation was
proved/checked and meets the release gates. A successful launch never reads
as proof coverage.

## 1.5.0 measured entries

The matrix published by Ligature 1.5.0 carries the P4 workspace
measurements made with Ligature 1.4.3. The publishing release carries them
as evidence; it does not relabel the 1.4.3 artifact.

| Verifier | Exact pins | Evidence level | Selection |
|---|---|---|---|
| Creusot | `cargo-creusot 0.12.0-dev` + `creusot-std 0.12.0-dev`; Rust `nightly-2026-04-21`; Why3/why3find and Alt-Ergo/Z3/cvc4/cvc5 pinned | intake + synthetic (synthetic obligation proved) | selectable, with a content-addressed wrapper/source NAR identity (no portable flake revision was retained for this control) |
| Verus | `0.2026.05.24.ecee80a`; Rust `1.95.0` | intake + synthetic (`success=true`, `verified=1`, `errors=0`) | selectable, with the pinned nixpkgs input and release-zip hash |
| Kani | `cargo-kani 0.67.0`; CBMC `6.8.0`; Rust `1.95.0` | intake + synthetic (synthetic harness successful) | selectable for planning; no portable Nix lock is claimed |
| Creusot declared shell | `cargo-creusot 0.14.0-dev` with `creusot-std 0.14.0`; Rust `nightly-2026-08-03` | intake only | **not selectable** — exact pair mismatch unresolved |

None of these entries establishes production source proof coverage or
resolves the 0.14 declared-shell mismatch (epic #2). The 0.14 row exists so
that the mismatch cannot be mistaken for the tested 0.12 pair.

## Per-project-root selection

The project descriptor may select one entry per independent root:

```json
{
  "toolchains": [
    {
      "project_root": ".",
      "verifier": "creusot",
      "matrix_entry": "creusot-0.12.0-dev-nightly-2026-04-21-x86_64-linux",
      "lock_id": "sha256:707d678219fd2a622f456ff86e4b0cd3de48ddf8bd3e5b3b5036ed53e86bfec6"
    }
  ]
}
```

`ligature toolchain check` rechecks the entry exists, is tested/selectable,
matches the declared verifier, has a compatible `cargo-creusot` /
`creusot-std` pair, matches an optional `lock_id`, and matches every
matrix-pinned node and input edge in an optional committed `flake.lock`.
Extra project-local flake inputs are allowed. A Verus selection may add
`"nix": { "lockfile": "flake.lock" }`; a Creusot 0.12 selection pins only the
retained store `lock_id`, because no portable flake revision exists for that
control. Nothing in the check writes a file or invokes Nix.

## Reproducible Creusot Nix identity

The selectable Creusot entry records the exact content-addressed store/source
identity that the measured control used:

- wrapper `/nix/store/9y9g4kpsppxn6zqmyfkvfcc2234qkdwv-creusot-wrapped`,
  NAR hash `sha256-JwDDUiHsS6SLOdIDI92hx92XY+Zqz+zxrtMJyRD0B4I=`;
- source `/nix/store/22j5g9088m140fgx2l6vbskkmg5ra5pp-creusot-source`,
  NAR hash `sha256-fflYrng0knZwLc57qGVZz3bZXEaLmWC1FsQBIPyZ8eI=`,
  content address `fixed:r:sha256:1qpik7y200f42ssn16cb8rfdjxngb5jshyyf5mq7d4ilg2p5iybx`;
- the matching `creusot-std 0.12.0-dev` source is the same retained source
  identity.

The retained Nix metadata does **not** record a portable flake revision for
this 0.12 control. The matrix therefore says so explicitly and pins the
content-addressed store/source identity instead of pretending the workspace's
0.14 flake input is a 0.12 pin. `ligature toolchain lock --entry <id>` prints
`ligature.toolchain-lock.v1` with that store identity and a `lock_id` computed
over the canonical record; `toolchain plan --entry <id> --json` prints the
same identity in a selection envelope. A project can pin `lock_id` in its
descriptor. `toolchain check` refuses a declared `nix.lockfile` for an entry
that retains no flake lock, rather than silently ignoring it.

The Verus entry does publish a flake-derived identity: its nixpkgs input is
locked by revision and NAR hash, and its release zip is pinned by a
fixed-output hash. `toolchain check` compares every matrix-pinned node and
input edge in a committed `flake.lock`; extra project-local inputs are
allowed.

A future Ligature release publishes a new matrix as a reviewed build input.
It must not edit or relabel an already-attested zipapp: the old artifact's
matrix digest and evidence hashes are part of its identity.

## Evidence

Retained evidence lives under
[`docs/verifier-compatibility-evidence/`](verifier-compatibility-evidence/).
The original pilot record is
[`LIGATURE-VERIFIER-COMPATIBILITY.md`](verifier-compatibility-evidence/LIGATURE-VERIFIER-COMPATIBILITY.md);
the probe results and version logs are linked from each matrix entry with
their exact SHA-256. A release bundles those files and recomputes their
hashes during `version --verify`/`doctor`.

A future Ligature release publishes a new matrix as a reviewed build input.
It must not edit or relabel an already-attested zipapp: the old artifact's
matrix digest and evidence hashes are part of its identity.
