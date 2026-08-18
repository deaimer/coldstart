# Task Authoring Guide

## Task contract

A ColdStart task must provide a realistic technical environment with an observable failure and an objectively verifiable target state. It should require investigation and interaction across multiple components rather than one obvious command.

A strong task includes:

- An unfamiliar but understandable system
- Seeded state that the agent must preserve
- Incomplete or misleading symptoms
- More than one plausible diagnostic path
- A repair that must survive a controlled perturbation
- Deterministic core verification
- Deliberately incorrect solutions used to test the verifier

## Native Harbor structure

```text
task-id/
├── instruction.md
├── task.toml
├── environment/
│   └── Dockerfile
├── solution/
│   └── solve.sh
└── tests/
    ├── test.sh
    └── test_outputs.py
```

Docker Compose may be used for multi-service systems. Multi-step Harbor tasks may be used when sequential phases need separate instructions or verification.

## Authoring workflow

1. Submit a short task proposal.
2. Confirm novelty, domain, difficulty target, and expected expert workflow.
3. Build the broken environment and seed realistic state.
4. Reproduce the symptoms manually.
5. Solve the task manually without relying on hidden knowledge.
6. Encode the reference procedure in `solution/solve.sh`.
7. Write deterministic tests for functionality, durability, and state safety.
8. Run the Oracle repeatedly from clean environments.
9. Run negative controls and intentionally incomplete solutions.
10. Submit for independent technical review.

## Evidence report

Tasks may require `/app/coldstart-report.json` with diagnosis, changes, tests performed, remaining risks, and evidence paths. Core correctness should still be established by deterministic verification.

## Prohibited patterns

Do not author tasks that:

- Depend on secret trivia or unavailable external information
- Accept only one arbitrary implementation
- Pass when the initial environment is untouched
- Reward deletion or replacement of valid state
- Expose the Oracle solution to the agent
- Depend primarily on an uncalibrated LLM judge
- Use flaky timing, uncontrolled internet resources, or unpinned mutable inputs
