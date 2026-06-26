You are a spec construction expert. Using two prior investigations of the SAME
bug, distill a single reusable **detection spec**. The codebase is your working
directory; you may Read/Grep to verify patterns.

The purpose of a spec is to encode **specific external knowledge from developers
that cannot be derived from code alone** (API semantics, field conventions,
service contracts, framework/runbook constraints). General patterns a plain LLM
review already catches have low value — prioritize the non-obvious, domain
knowledge that separates an expert reviewer from a generic scanner.

## The bug
File: {file}
Line: {line}
Reported description:
{description}

## Agent A — BLIND review trajectory (told only the location, NOT the bug)
This shows what a generic reviewer does and, crucially, what it MISSES. If Agent
A failed to find the bug, the spec must encode the knowledge it lacked.
<<<TRAJECTORY_A
{trajectory_a}
TRAJECTORY_A
>>>

## Agent B — INFORMED analysis trajectory (told the bug)
This contains the root cause, reachability analysis, and the domain knowledge.
<<<TRAJECTORY_B
{trajectory_b}
TRAJECTORY_B
>>>

## How to design the spec

**1. Analyze the pattern**: What is the root cause? Why did the BLIND agent miss
it (what domain knowledge was absent)? That missing knowledge is the spec's core.

**2. Generalization safety check**: Only generalize if the generalized pattern
preserves the original bug's root-cause mechanism. If generalizing would match
code where the mechanism does not apply, keep the spec NARROW (API/framework
specific) or add discriminating conditions. Prefer narrow & high-precision over
broad & high-FP.

**3. semantic_specification** (array of patterns/steps): Encode how to find the bug.
- Each step has a clear input -> output.
- Put domain knowledge directly in the step text.
- MUST require call-site / reachability analysis when the bug depends on input
  state (nil, zero value, empty, single-source, etc.) — a signature alone is
  never sufficient evidence.
- Each step should require listing evidence.

**4. syntactic_specification**: Array of ag/rg-compatible regexes for deterministic pre-screen.
  Escape regex chars in Go: `\\.` for `.`, `\\(` for `(`, `\\[` for `[`.
  Patterns are OR'd (any match triggers). They must ALSO compile as Python `re`
  (no variable-width look-behind, possessive quantifiers, or atomic groups).
  Derive them from the bug's key API calls, type names, or structural pattern.
  At least one pattern MUST match the buggy file at line {line}. Specific enough
  to filter most irrelevant files, loose enough not to miss true positives.


## Output
Output EXACTLY one spec as a ```json code block with this structure (no `id`
field — it is assigned later):

```json
{{
  "name": "short-descriptive-name",
  "description": "One paragraph describing the bug and its consequence.",
  "syntactic_specification": ["regex1", "regex2"],
  "semantic_specification": [
    "Pattern 1",
    "Pattern 2"
  ]
}}
```
