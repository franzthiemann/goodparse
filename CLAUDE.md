## Workflow Orchestration

### 1. Plan Mode Default
- For ANY task with 3+ steps, architectural decisions, or irreversible actions:
  write plan to tasks/todo.md, wait for approval before executing
- For simple tasks: act directly
- If something breaks: STOP, diagnose root cause, update plan

### 2. Subagent Strategy
- Spawn subagents for: research, exploration, parallel file analysis, test runs
- Keep main context clean — offload anything that doesn't need shared state
- One focused task per subagent; synthesize results in main context
- Prefer subagents for long searches or multi-file refactors

### 3. Self-Improvement Loop
- After ANY user correction: append the pattern to tasks/lessons.md
- Re-read tasks/lessons.md at the start of each session
- Ruthlessly eliminate recurring mistakes before they repeat

### 4. Verification Before Done
- Never mark complete without proving it works in the container
- Run code, check output, diff against expected behavior
- Ask: "Would I be embarrassed if this was wrong?"
- For environment changes: verify container still builds cleanly

### 5. Autonomous Execution
- Bug reports, failing tests, broken CI: just fix them
- Don't ask mid-task unless genuinely blocked
- Surface diffs and results, not process narration

### 6. Demand Elegance (Balanced)
- Non-trivial changes: pause and ask "is there a cleaner way?"
- Hacky fix detected: implement the proper solution instead
- Simple obvious fixes: don't over-engineer

## Task Management
1. **Plan First**: write checkable plan to tasks/todo.md
2. **Verify Plan**: check in before destructive or large-scope actions
3. **Track Progress**: mark items complete as you go
4. **Explain Changes**: high-level summary at each step
5. **Capture Lessons**: update tasks/lessons.md after any correction

## Container / Environment
- Never install packages globally; use the project venv/conda env
- If the container breaks: diagnose before touching Dockerfile or deps
- Environment changes (requirements.txt, Dockerfile) always require plan-mode
- Verify container builds cleanly after any env modification

## Code Standards (Python/Bash)
- **Minimal impact**: change only what's necessary
- **No hacks**: brittle solutions get replaced, not shipped
- **Reproducibility**: scripts must run top-to-bottom without hidden state
- **No placeholders**: never leave "fill this in later" stubs

## LaTeX
- Preserve document class, preamble, and bibliography style unless told otherwise
- Prefer semantic macros over hardcoded formatting
- Mentally compile-check before suggesting changes

## Scientific Context
- Exact over approximate; flag all assumptions explicitly
- If a result looks wrong: say so, never silently propagate errors
- Distinguish "this is standard" from "this is my suggestion"

## Core Principles
- **Simplicity first**: simplest correct solution wins
- **No laziness**: find root causes, no temporary patches
- **Precision**: in research context, wrong is worse than slow
- **Minimal footprint**: only touch what's necessary; no side effects
