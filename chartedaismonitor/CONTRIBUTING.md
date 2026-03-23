# Contributing and Git workflow

## Weekly rhythm

- Aim to **commit and push at least once per week** (e.g. end of week) so progress is visible and recoverable.
- Prefer **small, focused commits** over one huge dump before deadlines.

## Branches

- **`main`** — stable, working state for demos and deployment.
- **Feature branches** — `feature/short-description` (e.g. `feature/map-trails`, `fix/leaderboard-404`). Open a pull request into `main` when ready, or merge locally if you work solo.

## Commit messages

Optional but helpful: [Conventional Commits](https://www.conventionalcommits.org/)

- `feat: add vessel course to map`
- `fix: correct leaderboard API route`
- `docs: update self-host instructions`

## Secrets and environment

- **Never commit** `.env` or API keys (AISStream, AISHub, database passwords).
- Update **`.env.example`** when you add new variables so others know what to configure.
- Use `.gitignore` to exclude `.env`, local data dumps, and editor junk.

## Before you push

- Run **pytest** when you change backend ingest or API logic (`cd backend && pytest tests/ -v` with `DATABASE_URL` set).
- Run **frontend lint/build** when you change UI (`cd frontend && npm run build`).
- Rebuild the backend Docker image after API changes: `docker compose up --build -d backend`.

## Code review (teams)

- Keep PRs scoped to one feature or fix.
- Link issues or course requirements in the PR description when applicable.
