# Reviewing ColdStart Tasks

## Independence

An author may not approve their own task. Reviewers should inspect the instruction, environment, Oracle, verifier, negative controls, and complete agent trajectories.

## Review checklist

### Specification

- Is the objective realistic and unambiguous?
- Can an expert infer all required behavior from available evidence?
- Are multiple safe implementations accepted where appropriate?
- Is the task materially novel?

### Environment

- Does it build from a clean checkout?
- Is initial state deterministic?
- Are dependencies, images, and important fixtures pinned?
- Are resource and network requirements justified?

### Oracle and verifier

- Does the Oracle pass repeatedly?
- Does the untouched environment fail?
- Do incomplete, destructive, and shortcut solutions fail?
- Are restart and state-preservation checks real rather than simulated?
- Can grading assets be modified or inferred trivially?

### Agent calibration

- Do trajectories show genuine investigation?
- Are failures caused by the intended difficulty rather than infrastructure?
- Does the task distinguish systems rather than producing universal success or failure?

## Review decisions

Use one of four outcomes:

- **Accept:** Ready for the next version candidate.
- **Changes required:** Correctable issues are documented.
- **Recalibrate:** Valid task, but difficulty or verifier sensitivity is unsuitable.
- **Reject:** Ambiguous, unsafe, duplicated, irreparable, or invalid evaluation design.
