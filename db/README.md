# db/

Holds `jobsearch.duckdb`, the single-file job store. The file itself is gitignored. Only this folder and its `.gitkeep` are tracked, so every checkout has the directory ready to receive it.

The schema, views and macros are defined in `backend/ats/store.py` and created on first run. Migrations from older schema versions run automatically. If you run the sweep from more than one machine, point them at one shared file rather than keeping independent copies, because posting history (`first_seen_at`, `closed_at`) only accumulates in one place.
