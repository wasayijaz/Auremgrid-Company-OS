---
name: git-attribution-guard
description: Maintain this repository's enforced Git identity and attribution policy when editing or committing its code.
---

# Git Attribution Guard

Use this skill before preparing a commit in this repository.

- Run `powershell -ExecutionPolicy Bypass -File scripts/install-git-guard.ps1` after cloning or when verification is needed.
- Commits must use `Auremgrid <auremgrid@users.noreply.github.com>` for both author and committer.
- Do not add third-party attribution trailers to commit messages.
- Do not add the reserved third-party attribution reference to tracked content.
- Run `git diff --check` and confirm the hooks are active before committing.
- Never rewrite published history or force-push to remove old metadata without explicit user approval. Explain the impact and verify the exact branches first.
