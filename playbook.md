# ShopDirect TS Migration Playbook

## Goal

Migrate the assigned batch of JavaScript/JSX files to TypeScript/TSX. Add types without changing business logic. Keep changes scoped and minimal.

## File Conversion Rules

- Rename `.js` files to `.ts`.
- Rename `.jsx` files to `.tsx`.
- Update any directly necessary imports caused by file renames.

## Typing Rules

- Add function parameter and return types.
- Add React props `interface` definitions for each component.
- Type state variables, hooks, and service responses where needed.
- Prefer `interface` for object shapes.
- Prefer `unknown` over `any` when the type is unclear.
- Keep types co-located in the same file — do not create separate type files.

## Verification

1. Run `npx tsc --noEmit` and ensure zero errors.
2. Run the test suite if available (`npm test` or equivalent).
3. Fix all type errors before considering the batch complete.

## Constraints

- Do **not** change business logic.
- Do **not** perform unrelated refactors.
- Do **not** make stylistic, architectural, or cleanup changes unrelated to the migration.
- Do **not** edit files outside the assigned batch except for directly necessary import, type, or compile-fix updates caused by the migration.
- Keep code style consistent with the existing codebase.

## Final Output

- Summarize what was migrated (files renamed, types added).
- Note any blockers clearly if they remain.
- Open a PR with the changes if possible.
