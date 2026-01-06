---
name: review
description: Review code for regressions, duplications, and spec compliance. Use after completing significant implementations, or when asked to verify work. Can be invoked proactively after major changes.
allowed-tools: Bash, Read, Grep, Glob, Task
---

# Code Review

Comprehensive code review checking for regressions, duplicated logic, and specification compliance.

## Invocation Modes

### Mode 1: Working Tree Review (default)
```
/review
```
Reviews uncommitted changes in the working tree.

### Mode 2: Specific Files/Directories
```
/review --files app/services/ app/routes/news.py
```
Reviews specific files or directories regardless of git status.

### Mode 3: Commit Review
```
/review --commit abc123
/review --commit HEAD~3..HEAD
```
Reviews all files changed in a specific commit or commit range.

### Mode 4: Spec Compliance (Full Codebase)
```
/review docs/feature_spec.md
```
Reviews ALL code relevant to the specification document.

### Mode 5: Spec Section (Full Codebase)
```
/review docs/feature_spec.md#"Section Name"
```
Reviews ALL code relevant to a specific section of the spec. The section name should match a markdown heading in the document.

### Combined Modes
```
/review --commit abc123 docs/spec.md
```
Review files from a commit against a spec document.

## Review Process

### Step 1: Determine Scope

**Parse arguments:**
- No args: `git diff --name-only` (working tree)
- `--files <paths>`: use specified paths
- `--commit <ref>`: `git diff --name-only <ref>^..<ref>` or `git diff --name-only <range>`
- Spec path: read spec, find all relevant code in codebase

**If spec provided:**
- Read the spec document (or specified section)
- Identify all files in the codebase that implement the spec
- Use Grep/Glob to find relevant code paths

### Step 2: Regression Check

1. **Run tests**
   ```bash
   pytest tests/unit -q
   pytest tests/integration -q  # if touching DB/routes
   ```

2. **Analyze removals** (for working tree or commit modes)
   - Check `git diff` for removed functions/classes
   - Verify removed code isn't used elsewhere
   - Flag any removed error handling or validation

3. **Check for breaking changes**
   - API signature changes
   - Database schema changes
   - Configuration changes

### Step 3: Duplication Scan

For each new function or significant code block:

1. Extract key patterns (function names, logic patterns, string literals)
2. Search codebase for similar implementations:
   ```bash
   # Example: find similar function names
   grep -r "def.*similar_name" app/
   ```
3. Flag potential duplications with locations
4. Suggest consolidation if appropriate

### Step 4: Spec Compliance (if spec provided)

1. **Parse requirements** from the spec document
   - Extract explicit requirements (MUST, SHOULD, etc.)
   - Identify acceptance criteria
   - Note any constraints or edge cases mentioned

2. **Map requirements to code**
   - For each requirement, find implementing code
   - Verify the implementation matches the spec

3. **Gap analysis**
   - List any spec requirements not implemented
   - Flag any implementations that deviate from spec
   - Note any code that isn't covered by the spec (scope creep)

## Output Format

Structure the review as:

```markdown
## Review Summary
- **Scope**: [working tree / files: X / commit: abc123 / spec: document name]
- **Files reviewed**: N files
- **Tests**: PASSED / FAILED (with details)

## Regressions
- [ ] Issue 1: description (file:line)
- [x] No regressions found

## Duplications
- [ ] `function_name` in file1.py is similar to `other_func` in file2.py
- [x] No significant duplications found

## Spec Compliance (if applicable)
### Implemented
- [x] Requirement 1: implemented in file.py:123

### Missing
- [ ] Requirement 2: not found in codebase

### Deviations
- [ ] Requirement 3: spec says X, but code does Y

## Recommendations
1. Specific actionable recommendation
2. ...
```

## Section Parsing

When a section is specified with `#"Section Name"`:

1. Read the full document
2. Find the heading matching the section name (case-insensitive)
3. Extract content from that heading until the next heading of equal or higher level
4. Use only that content for spec compliance checking

Example:
```
/review docs/roadmap.md#"News Feed"
```
Would extract the "News Feed" section and all its subsections.

## Commit Range Examples

```bash
# Single commit
/review --commit abc123

# Last 3 commits
/review --commit HEAD~3..HEAD

# All commits on current branch vs main
/review --commit main..HEAD

# Specific range
/review --commit v1.0.0..v1.1.0
```

## When to Invoke Proactively

Consider running this review automatically when:
- Completing a feature implementation
- Making changes to core services or models
- Refactoring existing code
- Before suggesting the work is complete

Ask the user if they'd like a review after significant changes.
