# Database Migrations

This project uses [Alembic](https://alembic.sqlalchemy.org/) to manage schema and data migrations. Revisions are stored in
`alembic/versions/` and target the SQLModel metadata defined in the application.

## Common Commands

```bash
export DATABASE_URL="postgresql+asyncpg://USER:PASSWORD@HOST/DB?sslmode=require"
make mig.revision m="add new table"
make mig.up
make mig.down
make mig.history
make mig.current
```

Always run migrations against the correct Neon branch before deploying. For risky deployments, cut a new Neon branch from
production, apply migrations there, validate, and only then update the production `DATABASE_URL` secret.
