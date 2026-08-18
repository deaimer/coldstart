# Evaluation Policy

## Reproducibility

Every released result must identify:

- ColdStart dataset version
- Exact task digests
- Agent name and version
- Model identifier and provider
- System-level reasoning or effort settings
- Resource limits and timeouts
- Network policy
- Trial count
- Date of execution

## Trial policy

Use clean, independent environments. A failed run counts as zero when failure is attributable to the evaluated system. Confirmed benchmark-infrastructure failures should be documented and rerun under the same configuration.

Do not selectively rerun only unsuccessful trials. Any complete rerun must be declared and replace the full affected batch.

## Ranking policy

The primary metric is mean `coldstart_pass` across the frozen evaluation set. Also publish:

- Reliable Pass@5
- Durability failure rate
- State-loss rate
- Integrity failure rate
- Unsupported-claim rate
- Median completion time
- Median provider-reported cost when available

Controlled model results and native agent-system results must appear in separate tables.

## Private evaluations

Customer evaluations may remain confidential. Private results must still use versioned tasks, immutable run configurations, reviewable trajectories, and the same failure-accounting rules as public evaluations.

## Conflicts and disclosure

Disclose sponsorship, paid evaluations, benchmark-development assistance, and material relationships that could affect interpretation. A customer may review its private report before delivery but cannot alter scoring rules after seeing results.
