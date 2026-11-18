# Maintenance — new AI Office team project

Owner clarification, 2026-10-25 (Asia/Bangkok): there is no existing Maintenance
source. This will be a new project assigned to the AI team to evaluate its work.

Status (2026-11-18): backend T1-T6 is implemented; see backend/README.md.
Isolated PostgreSQL 17.12 verification passed 417 tests. Frontend and complete
UAT delivery remain unfinished; this is not a production release.
Source: D:\PCL Intelligence projects\PCL Mainetenance.
The owner requested relocation and publication; automation remains stopped.
Do not invent existing features, project progress, asset records or maintenance SOPs.

Known intended domain: asset management, preventive maintenance scheduling and
explicit maintenance state machines. SA must translate the owner's first scoped
assignment into acceptance criteria and surface consequential unresolved decisions.

Technology baseline: Next.js 15.5.25, React 19.0.4, TypeScript 5.7 strict/no any;
Python 3.12, FastAPI, PostgreSQL 17, SQLAlchemy 2.0 select()/session.execute(),
Alembic, psycopg 3, Docker Compose, pytest/httpx/Ruff when implementation begins.

Keep this system independent of SAI4/TVO/PIP databases, ports and source.
GitHub destination: https://github.com/teerp-jpg/pcl-mnt.
No complete UAT URL is claimed by this snapshot.
Human Orchestrator controls scope and acceptance. Existing source/GitHub must not
be deleted, emptied or renamed by deletion without explicit, specific approval.
No commit/push/deployment authority is granted to workers by this README.

## Existing planning context

Read docs/planning/sa-WI-261024-FE995636.md and the adjacent human-decisions JSON.
The owner already answered six scoped questions; reuse those answers.
Owner-selected GitHub destination: https://github.com/teerp-jpg/pcl-mnt.
The owner authorized this source snapshot to be committed and pushed on 2026-11-18.
This one-time publication does not grant autonomous workers GitHub write access.
