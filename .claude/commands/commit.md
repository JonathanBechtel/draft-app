# Commit and Push

Stage all changes, create a commit with a conventional commit message, and push to the remote.

## Instructions

1. Run `git status` to see what files have changed
2. Run `git diff` to understand the changes (both staged and unstaged)
3. Run `git log --oneline -5` to see recent commit message style
4. Stage relevant files with `git add`
5. Create a commit with a descriptive message following conventional commits format
6. Push to the current branch

## Important: Git Identity

Do NOT modify the git author or add Co-Authored-By headers. The commit should use the user's existing git configuration (their name and email from `git config user.name` and `git config user.email`).

## Commit Message Format

Use conventional commits style observed in the repository:
- `feat:` for new features
- `fix:` for bug fixes
- `chore:` for maintenance tasks
- `refactor:` for code refactoring
- `docs:` for documentation changes

Keep subject line under 72 characters. Focus on the "why" rather than the "what".

## Commit Command Format

Always use a HEREDOC to ensure proper formatting:

```bash
git commit -m "$(cat <<'EOF'
<type>: <description>

<optional body explaining why>
EOF
)"
```

## Safety Rules

- NEVER commit files that contain secrets (.env, credentials.json, etc.)
- NEVER use `--force` or `--no-verify` unless explicitly requested
- NEVER amend commits that have been pushed to remote
- NEVER modify git config (user.name, user.email, etc.)
- If pre-commit hooks fail, fix the issues and create a NEW commit

## Push

After committing, push to the current branch:

```bash
git push origin HEAD
```

If the branch doesn't have an upstream, use:

```bash
git push -u origin HEAD
```
