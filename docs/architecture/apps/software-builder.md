# Software Builder

Software Builder turns repository requests into focused pull requests on
connected GitHub repositories. It is a working and review surface for PRs, not
an app-package generator or installer.

The workspace starts with a pull-request goal and readiness measurement. The
human supplies a repository and desired change; the agent inspects the repo,
implements on a focused branch, verifies the change, pushes it, opens or updates
the PR, and follows review and CI until it is ready for the operator to merge.

## Pull request lifecycle

1. **Scope.** Confirm the repository, requested outcome, and important product
   choices that cannot be discovered from the code or issue context.
2. **Implement.** Work from current main on one focused branch and keep the diff
   limited to the request.
3. **Verify.** Run the repository's relevant local checks and record what each
   layer covers.
4. **Open or update.** Push through the connected GitHub path and create one PR,
   or add commits to the existing PR for the task.
5. **Review.** Track checks and every inline or issue comment, fix or answer each,
   and repeat until a full round is green and quiet.
6. **Hand off.** The operator merges. Software Builder never reports a PR as
   merged merely because it is ready.

## PR artifacts

Each durable artifact represents one pull request. It records repository,
branch, PR URL and number, summary, verification, check state, review threads,
remaining blockers, and merge readiness. The main workspace surfaces are the PR
lifecycle, definition of ready, and the collection of active and completed PR
artifacts. There are no package, manifest, migration, or installer stages.

GitHub is the required connection. Brave Search is optional for current public
technical documentation. Exact repository authority, network policy, protected
push approval, and merge authority remain host and operator decisions.

For the shared workspace behavior behind goals, messages, artifacts, schedules,
memories, tools, agent runs, and authentication, see [Workspace Kit](workspace-kit.md).
