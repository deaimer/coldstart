# ColdStart Methodology

## Benchmark objective

ColdStart evaluates whether autonomous AI agents can produce reliable repairs in unfamiliar, stateful technical systems. The benchmark emphasizes durable outcomes rather than superficial test passing.

## Core dimensions

1. **Functional correctness:** Required user-visible behavior works.
2. **Durability:** The repair survives restart, rebuild, migration, or a controlled perturbation.
3. **State safety:** Existing valid state is preserved or migrated correctly.
4. **Process integrity:** The agent does not bypass the intended problem, modify protected grading assets, or exploit verifier weaknesses.
5. **Evidence integrity:** Claims in the final report agree with the resulting system, files, and logs.

Root-cause reasoning may be reviewed, but a task must not require one exact implementation when multiple safe solutions exist.

## Primary result

Each trial emits a binary `coldstart_pass`. A trial passes only when every critical gate succeeds. Secondary numeric metrics explain failure modes but cannot compensate for state loss, integrity failure, or an undurable repair.

Recommended reward keys:

```json
{
  "coldstart_pass": 1,
  "functional": 1.0,
  "durability": 1.0,
  "state_safety": 1.0,
  "evidence": 0.9,
  "integrity": 1.0
}
```

## Evaluation tracks

ColdStart reports two separate tracks:

- **Controlled model track:** Models run through the same agent harness and resource policy.
- **Native agent-system track:** Complete products run with their own prompts, tools, and context-management systems.

Results from the two tracks must not be combined into one ranking.

## Repetition and reliability

Public benchmark results should use at least five independent trials per task and system. Report average pass rate and Reliable Pass@5, where the latter requires all five attempts to pass.

## Release design

The public repository may include development tasks and methodology. The scored evaluation split should remain private or access-controlled, use immutable version identifiers, and rotate as contamination becomes likely.
