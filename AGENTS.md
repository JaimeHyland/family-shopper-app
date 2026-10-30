# Standing user preference: strict cost discipline

> **MOST-MISSED RULE — CHECK THIS LAST, EVERY TIME:** Any message you are
> about to send that is a report (investigation report, diagnostic report,
> phase report, completion report, findings, root-cause analysis - any
> structured summary written up for the user) MUST be copied to the
> Windows clipboard via PowerShell `Set-Clipboard` in the SAME turn, before
> you stop and hand control back to the user. This has been missed
> repeatedly in the past. Before ending any turn that contains such a
> report, explicitly re-read the report you just wrote and ask yourself
> "did I already run Set-Clipboard on this exact content in this turn?" -
> if not, do it now, then finish.

Treat cost control as a high priority in every task and future session in this project.

- Keep work tightly scoped to the user's request. For simple tasks, take the
  shortest reliable path and give a brief answer.
- Minimize tool calls, context consumption, and output. Read only relevant
  portions of files; batch independent checks where useful.
- Reuse established findings. Do not repeat successful checks without a new
  change or concrete unresolved concern that justifies them.
- Run validation proportionate to the change. Avoid broad suites for wording
  changes or other low-impact edits unless explicitly requested or required.
- Before re-running a test (or suite), check whether anything relevant has
  changed since it last ran. If nothing relevant changed, reuse the prior
  result instead of re-running it.
- Immediately before running any battery/suite of tests (not just a single
  test), explicitly assess whether that repetition is absolutely necessary -
  e.g. whether the exact suite, or a superset covering it, already ran
  fresh and passed since the last relevant change. If so, skip the rerun
  and say so, rather than running it again "to be safe."
- Always copy every report (investigation reports, phase reports,
  completion reports, findings, root-cause analyses - anything written up
  as a structured summary for the user) to the Windows clipboard in plain
  text automatically once produced, without waiting to be asked and
  without being reminded. This applies every single time, with no
  exceptions. Use a single PowerShell `Set-Clipboard` call with a literal
  here-string (`@'...'@`), not an expandable one (`@"..."@`), so `$` and
  backtick characters in the report (e.g. code snippets) are not
  corrupted by interpolation. This should be a one-step operation; do not
  add multi-step verification beyond a simple length/spot-check when the
  content has no special characters.
- Avoid frequent polling. Use sensible wait intervals and concise updates.
- Do not delegate to additional agents unless explicitly requested.
- Before expanding a task into substantial extra investigation or expensive
  work, explain why and obtain the user's agreement. Do not ask for routine
  confirmations for already authorized work.
- Never claim to know account charges or guarantee a spending cap without
  access to billing information and actual enforceable budget controls.

These instructions do not authorize skipping necessary safety checks or
misrepresenting incomplete work as verified. Do not commit or push this file
unless the user requests it.

## Architectural analysis before implementation

Before making substantial changes, analyse the existing architecture, data structures, and relevant code paths first. Determine whether the existing structures can support the requirement and identify reasonable implementation options before introducing new models, fields, abstractions, or dependencies. Prefer the simplest solution that fits the existing architecture. Do not make speculative architectural changes.

## Backend before frontend

For features involving both backend and frontend, implement and validate the backend/API layer before beginning frontend work, where practical. The backend should expose the information and behaviour the frontend needs; the frontend should not unnecessarily reconstruct backend/domain logic. Begin frontend implementation only after the appropriate backend tests have passed.

## Phase boundaries

For multi-phase tasks, work in clearly defined phases and report the outcome of each phase before proceeding to the next. Do not silently move from investigation into implementation or from one architectural layer into another.

Do not introduce complexity solely to accommodate hypothetical future requirements. Prefer deferring genuinely disproportionate complexity to a later iteration.

## Development Cycle and Project Stage

Always establish the current development stage before acting. The normal cycle is:

1. **Implement** — make the requested code changes and create or update migrations as needed.
2. **Test in development** — run relevant automated tests, apply pending migrations to the confirmed development database when appropriate, and verify the resulting behaviour.
3. **Review** — inspect the changes, address failures, and establish that the implementation is ready to commit.
4. **Commit** — normally after each completed and accepted task, with explicit authorisation. Push is a separate operation requiring separate authorisation.
5. **Deploy and migrate production** — only when explicitly requested or authorised as a production deployment task.


## Git discipline and incremental commits

Treat Git commits as routine development checkpoints, not as an end-of-project activity.

### Rules

- Keep the working tree focused on one agreed architectural task or coherent feature at a time.
- Do not allow unrelated completed work to accumulate as uncommitted changes.
- Once a task has been implemented, appropriately tested, reviewed and accepted, prepare a thematic commit before beginning the next independent task.
- Normally, one completed architectural task should produce one commit. Substantial tasks may require multiple commits where they represent coherent, independently reviewable stages.
- Each commit should represent one logical change. Keep related implementation code, tests and migrations together. Never separate a migration from the model or feature changes it supports.
- Before staging, inspect the working-tree status and identify unrelated, local-only or generated files that must be excluded.
- Stage one thematic group at a time. Inspect the staged diff and show the user what will be included before committing.
- Do not stage unrelated changes merely because they are already present in the working tree. Use selective or partial staging where necessary.
- Use concise, descriptive commit messages that explain the purpose of the change.
- After committing, check that the intended changes were committed and report the resulting commit hash and any remaining uncommitted changes.
- Before starting a new independent architectural task, normally ensure that the previous task has been committed and the working tree is clean. If unrelated or uncommitted changes remain, stop and resolve them rather than silently incorporating them into the next task.

### Authorisation

- Implementation, testing and review do not automatically authorise staging or committing. Obtain the user's approval before each commit unless the current task explicitly authorises a defined series of commits.
- Local commits do not authorise pushing to a remote repository.
- Pushing does not authorise deployment.
- Treat staging, committing, pushing and deploying as separate operations requiring their own authorisation.
- Never amend, squash, rebase or rewrite existing commits without explicit authorisation.

### Objective

Maintain a short, comprehensible history of independently reviewable changes. Avoid large accumulated diffs that are expensive to inspect, difficult to attribute and risky to commit.

### Rules

- Treat the current task and the actual state of the repository as authoritative. Do not assume that development work is a production deployment exercise.
- Keep track of the current stage and report it briefly when it materially affects the next action.
- Do not advance to another stage without the user's instruction or clear authorisation in the current task.
- New or modified migration files that are not yet committed are normal during implementation. Their absence from Git history is not, by itself, a defect.
- Before applying migrations or changing data, establish which database is targeted. Never assume that local settings point to development, or that a database is safe to modify.
- Never access or modify production data, apply production migrations, deploy, commit, or push unless the current task explicitly authorises the relevant action. Authorisation to commit may cover a specifically defined series of thematic commits, but does not automatically authorise other commits or any push. Pushing and deploying always require separate explicit authorisation.
- Do not introduce production-readiness audits, deployment procedures, backups, or other ancillary work into an ordinary development task unless they are necessary to perform that task safely.
- When a task fails, investigate within its authorised scope. Do not improvise destructive repairs, alter migration history, or expand the task without approval.
- Preserve useful existing data and project conventions unless the task explicitly calls for a reset or replacement.
- At the end of each task, report what changed, what was tested, the actual results, the current development stage, and the next logical step. Clearly distinguish completed work from recommendations.

The objective is to progress through the development cycle deliberately, without prematurely treating uncommitted development work as a production release.

## Efficient use of project snapshots

When a project ZIP or other compact source snapshot is available, use it as a reference for understanding the architecture and locating relevant code whenever doing so is more economical than exploring the working tree.

Inspect the archive's file listing and relevant files selectively; do not read the entire application unnecessarily.

Use the snapshot to establish context, trace existing patterns and identify likely implementation points.

Before editing, verify the relevant files in the actual working tree. The working tree is authoritative for implementation.

If the snapshot and working tree differ, follow the working tree and investigate the discrepancy only where it matters.

Never assume a sanitised snapshot contains every file or all original configuration values.

Keep investigation, tool calls, output and validation proportionate to the task.

## Password-string masking in tool output

Tool output in this environment (view/grep/powershell alike) redacts
password-like literals, rendering them as a literal `******` placeholder.
This masking happens only at the display layer; it does not reflect what
is actually stored in the file.

This has caused real bugs more than once: copying a masked `******` from
viewed/grepped existing test code (e.g. `create_user(username="x", ******)`)
into *new* code via create/edit produces invalid Python (`******` is not
valid syntax) because the edit tool writes exactly what it's given, with
no masking applied on write.

Workaround:
- Never retype or reuse a masked `******` you see in tool output as part
  of new code you are writing.
- When `old_str`/`new_str` must reproduce an existing masked line
  unchanged (e.g. moving or wrapping a line without changing its
  content), copy it verbatim including the `******` placeholder -- the
  underlying file content round-trips correctly as long as you don't
  retype it.
- When writing brand-new lines that need a password-like argument (e.g.
  `User.objects.create_user(..., password="...")`), always supply your
  own explicit literal (e.g. `password="pass1234"`) rather than trying to
  reproduce the masked value.
- After any edit that introduces or touches a password-like literal,
  syntax-check the file (e.g. `python -c "import ast; ast.parse(open(path).read())"`)
  before relying on it, since a stray `******` will otherwise only surface
  as a confusing runtime/syntax error later.
