# ligature

`ligature` is a CLI for porting a codebase to Rust with LLM help, without
letting the LLM decide whether its own work is correct.

LLMs draft the specifications: evidence, concepts, contracts, and the
**reliance graph**, which records what each component relies on from each
other component. Deterministic gates then check those drafts, a human
approves them, and a hash-pinned work-package manifest freezes the write set
so that implementation work cannot quietly weaken a spec. A verification
shortfall becomes a change request and goes back into the loop. Nothing
downstream patches around it.

> **Scope:** ligature has only been tested on **C++ → Rust** ports. The
> approach (reliance graphs, a determinism boundary between LLM proposals and
> mechanical gates, human promotion checkpoints, change-request feedback)
> doesn't depend on either language and could be extended to other
> source/target pairs. This repository **does not support** other languages.

## Workflow

```mermaid
flowchart TD
  subgraph LLM["LLM side: proposes, non-normative"]
    S0["Stage 0: Evidence intake"] --> S12["Stages 1–2: Concepts & intra-concept contracts"]
    S12 --> S3["Stage 3: Interactions, reliance edges,<br/>bridge & witness specs"]
  end

  S3 --> H1{{"Human checkpoint<br/>ligature approve"}}
  H1 --> S4["Stage 4: Adjudication<br/>deterministic gates (G1–G18), no LLM"]
  S4 -->|pass| P45["Stage 4.5: Promotion<br/>detached receipt + artifact manifest"]
  P45 --> S7["Stage 7: Work-package manifest<br/>write set frozen, gate code hash-pinned"]
  S7 --> IMPL["Implementation<br/>one issue → one worktree → one PR<br/>suppliers before callers"]
  IMPL --> S8A["Stage 8A: Implementation verification<br/>call sites, bridges, R1/G16, witnesses"]
  S8A --> S8B["Stage 8B: Acceptance<br/>differential testing vs. C++ original"]
  S8B --> S8C["Stage 8C: Release closure (G14)<br/>closure_kind or degradation record"]

  S4 -->|fail| FB["Findings & change requests"]
  S8A -.->|unprovable / missing contract| FB
  S8C -.->|shortfall / drift| FB
  FB -.->|targeted evidence backfill| S0

  classDef llm fill:#fdf1e3,stroke:#c77b30;
  classDef det fill:#e6f0fb,stroke:#3a6ea5;
  classDef fb fill:#fbe7e7,stroke:#b03a3a;
  class S0,S12,S3 llm;
  class S4,P45,S7,S8A,S8C det;
  class FB fb;
```

Everything below the human checkpoint is deterministic. No gate ever calls an
LLM, because a gate that could ask an LLM "does this look right?" would let the
LLM's own proposals certify themselves. The feedback edges carry the loop:
gate failures, unprovable obligations, and closure shortfalls all become
change requests that re-enter at Stage 0 with targeted evidence.

Stages 5 (stub emission) and 6 (attach gate) are designed but not yet
implemented. They're left out of the diagram above.

## Quick start

Requires Python ≥ 3.10.

```sh
pip install -r requirements.txt           # jsonschema, PyYAML
python3 scripts/build_zipapp.py --out dist
python3 dist/ligature.pyz init --mode port   # bootstrap a workspace
python3 dist/ligature.pyz check              # read-only status + next action
```

The LLM backend is pluggable (`claude -p`, `codex exec`, `opencode run`, or a
manual mode) and is configured in the project descriptor that `init` writes.

## Further reading

- [`docs/mode-p-cli-flow.md`](docs/mode-p-cli-flow.md): every CLI command mapped to its stage
- [`docs/cli-contract.md`](docs/cli-contract.md): command grammar and semantics
- [`docs/trust-and-compatibility-boundaries.md`](docs/trust-and-compatibility-boundaries.md): read/write authority
- [`plan.md`](plan.md): full design rationale
- `vendor/concept-to-code`: the concept/contract skill that covers layers 1–2 (git submodule)

## License

MIT, see [`LICENSE`](LICENSE). The `vendor/concept-to-code` submodule is a separate repository with its own terms.
