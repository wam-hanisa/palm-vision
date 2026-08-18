# Palm Vision — Python/Django rebuild

Start here, in order:

1. **`BUILD_STATUS.md`** — what's actually built and verified vs. still open.
2. **`CLAUDE.md`** — non-negotiable design decisions and project conventions.
3. **`docs/02-build-plan.md`** — the full 9-phase build plan.

## Run it locally

```bash
cp .env.example .env
docker compose up -d --build
docker compose exec web python manage.py migrate
docker compose exec web python manage.py createsuperuser   # or:
docker compose exec web python manage.py seed_demo_data    # demo farm/blocks/trees + 3 users
docker compose exec web pytest -v
ruff check .                                                # lint — see pyproject.toml
```

App: http://localhost:8000/admin/ · API root: http://localhost:8000/api/ · PWA field client: http://localhost:8000/field/ · Office dashboard: http://localhost:8000/dashboard/ (Manager/Super Admin login — see BUILD_STATUS.md for what's built there so far)

Full walkthrough (prerequisites, PostGIS sanity check, offline testing,
backup/restore drill, common issues): `docs/11-local-development.md`.

Demo users after `seed_demo_data` (local dev only — never reuse these
passwords anywhere real): `admin`/`admin12345` (Super Admin),
`manager`/`manager12345` (Manager), `pic_block`/`picblock12345` (PIC Block).
