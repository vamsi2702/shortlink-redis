# Diagrams: progress so far, Alembic, and the database

## 1. Work done so far

```mermaid
flowchart TD
    subgraph P0["Phase 0: Setup"]
        A1["0.1 Tools installed<br/>Python, Git, Docker, VS Code"] --> A2["0.2 Skeleton<br/>folders, pyproject.toml, .venv, git init"]
        A2 --> A3["0.3 pytest + ruff<br/>1 dummy test passes"]
        A3 --> A4["0.4 docker-compose.yml<br/>Postgres 16 running"]
    end
    subgraph P1["Phase 1: Backend"]
        B1["1.1 Hello API<br/>api/main.py: GET /health"] --> B2["1.2 Database and models<br/>models.py + Alembic migration"]
        B2 --> B3["1.3 Shorten a link<br/>(next)"]
    end
    A4 --> B1
    style B3 stroke-dasharray: 5 5
```

## 2. How Alembic works

```mermaid
flowchart LR
    M["api/models.py<br/>(Python classes)"] -->|"alembic revision --autogenerate<br/>compares models to the database"| V["api/migrations/versions/<br/>e992f6c2668d_create_users_links_clicks.py<br/>(instructions: CREATE TABLE ...)"]
    V -->|"alembic upgrade head<br/>runs the instructions"| DB[("Postgres<br/>users, links, clicks<br/>alembic_version")]
    DB -.->|"alembic_version remembers<br/>which migrations already ran"| V
```

## 3. ER diagram (the database tables)

```mermaid
erDiagram
    USERS ||--o{ LINKS : "owns"
    LINKS ||--o{ CLICKS : "receives"

    USERS {
        int id PK
        string email UK
        string password_hash
        datetime created_at
    }
    LINKS {
        int id PK
        string code UK "short code, e.g. abc12"
        string long_url
        int user_id FK "optional for now"
        datetime created_at
    }
    CLICKS {
        int id PK
        int link_id FK
        datetime clicked_at
    }
```

How to read it: `||--o{` means "one on the left, zero or many on the right". One user owns many links, and one link receives many clicks. PK is the primary key, UK is unique, FK is a foreign key.

## 4. Example rows

**users**

| id | email | password_hash | created_at |
|---|---|---|---|
| 1 | asha@example.com | $2b$12$Xk3... (a hash, never the real password) | 2026-10-08 10:00 |

**links**

| id | code | long_url | user_id | created_at |
|---|---|---|---|---|
| 1 | abc12 | https://example.com/a/very/long/path?x=1&y=2 | 1 | 2026-10-08 10:05 |
| 2 | k9Zq | https://docs.python.org/3/library/asyncio.html | NULL (made without logging in) | 2026-10-08 10:07 |

**clicks**

| id | link_id | clicked_at |
|---|---|---|
| 1 | 1 | 2026-10-08 11:00 |
| 2 | 1 | 2026-10-08 11:02 |
| 3 | 2 | 2026-10-08 11:05 |

Reading the example: Asha owns link `abc12`, which was clicked twice. Link `k9Zq` has no owner and was clicked once.

## 5. Example queries (what the app will do later)

```sql
-- Redirect (step 1.4): find where a code points. Fast because `code` has a unique index.
SELECT long_url FROM links WHERE code = 'abc12';

-- A user's links (step 1.6)
SELECT code, long_url FROM links WHERE user_id = 1;

-- Click count per link (analytics, Phase 4)
SELECT l.code, COUNT(c.id) AS clicks
FROM links l LEFT JOIN clicks c ON c.link_id = l.id
GROUP BY l.code;
```

The same thing in SQLAlchemy, as used in `api/tests/test_models.py`:

```python
link = db.scalar(select(Link).where(Link.code == "abc12"))
print(link.long_url, len(link.clicks))   # https://example.com/... 2
```
