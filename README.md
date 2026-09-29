# Fret Coach API — FastAPI learning port

This folder is a Python rewrite of the NestJS API in `../fret-coach-api`. It keeps the `/api/v1` route names and the main request/response behavior so you can compare the two implementations.

## Run it

```sh
cp .env.example .env
uv run python seed.py
uv run fastapi dev app/main.py --port 3000
```

The API is at `http://localhost:3000/api/v1`; interactive docs are at `http://localhost:3000/api/docs`. SQLite creates `fret_coach.db` in this folder the first time the app starts. The seed adds four original exercises with generated timelines. No source database or source-repository files are changed.

## Tests

```sh
uv run pytest
```

Each test uses its own temporary SQLite file; Supabase Storage and Google sign-in are replaced with fakes, so no network access or `.env` is needed.

## What to compare while learning

- FastAPI route declarations and Pydantic request validation: `app/main.py`, `app/schemas.py`
- SQL and transactions: `app/database.py`
- Password hashing, signed tokens, and Google ID-token checks: `app/security.py`
- AlphaTex and MusicXML normalization into the versioned score timeline: `app/score_timeline.py`
- Private Supabase Storage upload/download/delete requests: `app/storage.py`
- Seed data: `seed.py`

## API surface

- `GET /health`
- `POST /auth/register`, `/auth/login`, `/auth/google`, `/auth/refresh`, `/auth/logout`
- `GET/PATCH /users/me`
- `GET /exercises`, `/exercises/:id`, `/exercises/slug/:slug`, `/exercises/:id/progress`
- `GET /dashboard/me`, `GET /progress/me`
- `POST /practice-sessions`, `/practice-sessions/:id/recordings`, `/practice-sessions/:id/complete`
- `POST/GET /recordings/:recordingId/analysis`
- `POST /practice-attempts`, `/practice-attempts/telemetry`, `/practice-attempts/:id/corrections`
- `GET /practice-attempts`, `/practice-attempts/:id`, `/practice-attempts/private/:scoreImportId/history`
- `POST /score-imports`, `/score-imports/:id/complete`; `GET /score-imports`, `/score-imports/:id`, `/score-imports/:id/source`; `DELETE /score-imports/:id`

All paths above are under `/api/v1`. Health, login/register/Google/refresh, and published exercise reads are public; the remaining routes require a bearer access token. Attempt aggregates are computed on the server, retries use `clientRunId`, and private imports/history are scoped to their owner.

## Learning-port boundaries

- The source service uses PostgreSQL and Prisma. This standalone copy uses Python's built-in SQLite driver to stay runnable with the dependencies already present in this folder. It does not connect to, migrate, or change the source database. To point a production port at PostgreSQL, replace the small SQL layer with a PostgreSQL driver and map these tables to the Prisma schema.
- The local user table uses PBKDF2 password hashes, so source NestJS bcrypt hashes are not portable into this learning database. Register a local account for this copy.
- Score timelines can be generated from the source service's AlphaTex seed format and from MusicXML. Binary Guitar Pro parsing is not ported; the file format is recognized at import creation, then completion records a clear `FAILED` parse diagnostic. Export that score as MusicXML or AlphaTex for the Python implementation.
- Audio analysis remains an asynchronous contract only, matching the source: queueing creates a `QUEUED` row; no transcription or analysis worker is included.
- Google ID tokens are checked against Google's `tokeninfo` endpoint in this learning version. Use Google's supported server-side verification library for a production service.
- Supabase score uploads use a private bucket and signed URLs. Keep `SUPABASE_SECRET_KEY` server-side, and create the `fret-coach-scores` bucket as private. The upload and download links expire after ten minutes.

This project is for comparing framework and language implementations, not a drop-in deployment replacement for the NestJS service.
