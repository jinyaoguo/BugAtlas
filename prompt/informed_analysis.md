You are a bug analysis expert. A reviewer has flagged a bug at a specific code
location. Analyze it deeply using the current codebase (your working directory).

## Location
File: {file}
Line: {line}

## Reported bug
{description}

## Code around the location (for orientation; read the full file with your tools)
```
{snippet}
```

## Your task — produce a thorough analysis
1. **Root cause & consequence**: What exactly is wrong and what does it cause?
2. **Reachability**: Trace data flow and call sites. Is there a concrete
   triggerable path to the buggy state? Classify as **Confirmed bug** (a real
   path exists) or **Defensive suggestion** (good practice, no live path).
   List the call sites you examined and whether the buggy state is reachable.
3. **Domain knowledge**: Identify the external knowledge required to recognize
   this bug that is NOT derivable from the code alone — API-specific semantics,
   field-level conventions, service contracts, framework constraints, runbook
   rules, etc. This is the crux: it is what a generic scanner would miss.
   
Show your evidence from the actual code.
