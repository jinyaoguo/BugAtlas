You are a spec refinement expert. A detection spec is producing FALSE POSITIVES.
Tighten it so those false positives disappear, WITHOUT losing its ability to
catch the true bugs it currently detects. The codebase is your working
directory; you may Read/Grep to verify.

## Current spec
```json
{spec_json}
```

## FALSE POSITIVES to eliminate
Each was reported by this spec but a validator judged it NOT a real production
bug. For each, work out WHY the spec wrongly fired — which `syntactic_specification`
pattern was too broad, or which `semantic_specification` pattern judged too loosely.
{fp_block}

## TRUE POSITIVES that MUST remain detected (recall guard)
Tightening must NOT exclude any of these real bugs. If a change would stop the
spec from flagging one of them, choose a more surgical change.
{tp_block}

## How to tighten
- **syntactic_specification**: make patterns more specific (add the qualifying
  API/type/structural context) so irrelevant files stop matching. Keep at least
  one pattern that still matches every TRUE-POSITIVE file. Patterns must compile
  as **Python `re`** regexes — no variable-width look-behind, no possessive
  quantifiers, no atomic groups; stick to basic regex + fixed-width look-around.
- **semantic_specification**: add explicit "NOT a bug if ..." exclusion conditions
  that cover each false positive's situation (call-site guarantees, framework
  contracts, test/CLI/backfill paths, single-source-by-design, etc.), and/or
  require stronger evidence (call-site / reachability analysis). Keep the patterns
  that catch the true positives.
- Prefer narrow & high-precision over broad & high-FP. Do not weaken detection
  of the true positives just to be safe.

## Output
Output the COMPLETE updated spec as a single ```json code block, same structure
and SAME `name` as the input spec (fields: name, description,
syntactic_specification, semantic_specification). Adjust only what is needed to
remove the false positives while preserving the true positives.
