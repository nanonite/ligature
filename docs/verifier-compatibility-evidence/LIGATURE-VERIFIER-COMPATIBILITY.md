# Ligature and verifier compatibility record

Status: workspace-local measurements, not an upstream Ligature support claim.
Recorded 2026-11-14 for the P4 workspace setup and the operator's direction
that each project remain an independent Git workspace and that Ligature should
provide its pinned verifier environment and publish the verifier versions it
has been tested with.

## Operator direction

The requested design is:

- each P4 project root owns its own Git history and reproducible verifier
  environment;
- the matching Creusot/`creusot-std` Nix pair is supplied through Ligature's
  workspace setup, rather than selected implicitly from the ambient shell;
- a Ligature release states the exact verifier and compiler versions against
  which that release was exercised.

This record captures that requirement and the evidence available today. It
does not claim that Ligature 1.4.3 implements the Nix environment or publishes
an official compatibility matrix.

## Ligature artifact identity

All four independent roots contain the same checksum-verified bundle:

- Ligature CLI: `1.4.3`, attested zipapp.
- Bundle SHA-256: `1a7a50eab31584946d8a7035ac3633130b0dc52d473d2587f3cd26e2800fd4e1`.
- CLI content hash: `sha256:bfc93caff9f5cbcfb62b9d12ab4c3761035589e7c9051683e8f2df474a5927bb`.
- Source commit printed by `ligature version`: `89033eee6f0415e32ec1a2287110a1fb828dd0a8`.
- `ligature version` reports Python/platform/artifact data, but no tested
  verifier matrix. The bundled `project-descriptor` schema has no tested
  verifier-version field, and the CLI has no `toolchain` or `verifier`
  subcommand. This is an observed 1.4.3 limitation, not a guess about future
  Ligature releases.

The exact version output and the four bundle hashes are retained in
`evidence/ligature-version-1.4.3.txt` and
`evidence/ligature-bundle-hashes-1.4.3.txt`.
The full top-level help, descriptor-schema export and no-such-subcommand
controls are retained in `evidence/ligature-cli-capabilities-1.4.3.txt`.
The root-local setup and compatibility notes are hashed in
`root-compatibility-docs.sha256` because the project roots are independent
Git repositories and have not yet been committed.

## Measured matrix

| Tool | Exact environment | Evidence and result | Qualification |
|---|---|---|---|
| Creusot | `cargo-creusot 0.12.0-dev`; `creusot-std 0.12.0-dev`; Rust `nightly-2026-04-21` (`rustc 1.97.0-nightly`, `66da6cae1`); Why3 `1.8.2+git`, why3find `1.3.0+dev`; Alt-Ergo `2.6.3`, Z3 `4.15.3`, cvc4 `1.8`, cvc5 `1.3.1` | #159 intake evidence and #160 same-function Kani overlay; #177 synthetic probe: `Proved (verif/p4_setup_creusot_probe_rlib/increment.coma) ✔`, exit 0 | Proves the synthetic obligation and tested intake/layout only. It does not validate the declared 0.14 shell or any P4 production contract. |
| Verus | `0.2026.05.24.ecee80a`; Rust `1.95.0` | #159 intake; #160 separate Verus crate; #177 synthetic probe: `success=true`, `verified=1`, `errors=0`, exit 0 | Synthetic and intake-path evidence only; not a P4 source proof. |
| Kani | `cargo-kani 0.67.0`; CBMC `6.8.0`; Rust `1.95.0` | #159 intake with exact harness selection; #160 shared executable-function overlay; #177 synthetic harness: `0 of 3 failed`, `VERIFICATION:- SUCCESSFUL`, exit 0 | Bounded harness evidence only; not deductive coverage or a P4 source proof. |

These are measured versions used in the workspace's Ligature and coexistence
experiments. Treat them as the current pilot matrix, not as a guarantee from
the Ligature 1.4.3 publisher. The evidence links are listed below.

## Creusot Nix reproducibility gap

The matching local control used the Nix store wrapper
`/nix/store/9y9g4kpsppxn6zqmyfkvfcc2234qkdwv-creusot-wrapped` and the matching
`creusot-std 0.12.0-dev` source. Retained Nix path metadata identifies the
wrapper NAR hash `sha256-JwDDUiHsS6SLOdIDI92hx92XY+Zqz+zxrtMJyRD0B4I=` and
source NAR hash `sha256-fflYrng0knZwLc57qGVZz3bZXEaLmWC1FsQBIPyZ8eI=`, but the
source store path has no recorded deriver or recoverable upstream revision in
that metadata. Those hashes identify the local store outputs; they are not a
portable Nix flake pin by themselves.

The declared workspace shell remains a separate, unresolved
`cargo-creusot 0.14.0-dev` / `creusot-std 0.14.0` mismatch on epic #2. The
successful 0.12 control does not validate that shell. Do not use or describe
the existing 0.12 package as a resolution of #2 until Ligature supplies a
reproducible expression/lock and the declared shell is checked against it.

## Required Ligature capability before production use

The next Ligature setup/version contract should carry, per independent root:

1. a pinned Nix expression or flake input and lock for each verifier toolchain,
   including Creusot and its matching `creusot-std`;
2. exact compiler, verifier, solver and platform versions, with immutable source
   revisions/content hashes;
3. a version-level compatibility manifest surfaced by `ligature version` or
   `doctor`, linking each tested pair to retained test evidence;
4. the distinction between a successful tool launch, a synthetic proof, a
   Ligature evidence-intake test and production-port proof coverage;
5. per-root Cargo locks, toolchain pins, target directories and durable proof
   output roots, without making the independent project roots share compiler
   caches or locks.

The 0.14 mismatch and unknown reproducible source pin remain prerequisites.
No flake, lock, descriptor, Cargo file, production source, grant or acceptance
was created by this compatibility record.

## Evidence references

- `evidence/creusot-0.12-probe/` — P4 synthetic Creusot command, version,
  compile/prove output, generated-file inventory, Nix path information and
  Why3 socket trace.
- `evidence/verus-1.95-probe/` — P4 synthetic Verus result.
- `evidence/kani-0.67-probe/` — P4 synthetic Kani result and log.
- `root-compatibility-docs.sha256` — hashes the setup and compatibility notes
  stored inside each independent Git root.
- `evidence/ligature-cli-capabilities-1.4.3.txt` — CLI help, descriptor schema
  export, and exit-2 diagnostics for unavailable `toolchain`/`verifier`
  commands.
- `crypto-mixed/analysis/p4-verifier-fit/p4-5-probes/continuation-1.4.3/README.md`
  — #159 Ligature 1.4.3 intake and closure experiments.
- `crypto-mixed/analysis/p4-verifier-fit/p4-6-coexistence-2026-10-25/README.md`
  — #160 independent Cargo-root coexistence and same-executable Creusot/Kani
  result.
- `VERIFIER-MATRIX.md` — portfolio-wide scope and links to this matrix.

