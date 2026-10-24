# Maintenance — new AI Office team project

Owner clarification, 2026-10-25 (Asia/Bangkok): there is no existing Maintenance
source. This will be a new project assigned to the AI team to evaluate its work.

Status: planned. This folder only prepares a clean, independent project workspace.
There is no application, database, deployed service, or passing business test yet.
Do not invent existing features, project progress, asset records or maintenance SOPs.

Known intended domain: asset management, preventive maintenance scheduling and
explicit maintenance state machines. SA must translate the owner's first scoped
assignment into acceptance criteria and surface consequential unresolved decisions.

Technology baseline: Next.js 15.5.25, React 19.0.4, TypeScript 5.7 strict/no any;
Python 3.12, FastAPI, PostgreSQL 17, SQLAlchemy 2.0 select()/session.execute(),
Alembic, psycopg 3, Docker Compose, pytest/httpx/Ruff when implementation begins.

Keep this system independent of SAI4/TVO/PIP databases, ports and source.
No GitHub remote or service health URL has been configured for this new project.
Human Orchestrator controls scope and acceptance. Existing source/GitHub must not
be deleted, emptied or renamed by deletion without explicit, specific approval.
No commit/push/deployment authority is granted to workers by this README.
