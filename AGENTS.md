# Standing user preference: strict cost discipline

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
- Always copy investigation reports to the Windows clipboard in plain text
  automatically once produced, without waiting to be asked. Use a single
  PowerShell `Set-Clipboard` call with a literal here-string (`@'...'@`),
  not an expandable one (`@"..."@`), so `$` and backtick characters in the
  report (e.g. code snippets) are not corrupted by interpolation. This
  should be a one-step operation; do not add multi-step verification
  beyond a simple length/spot-check when the content has no special
  characters.
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
