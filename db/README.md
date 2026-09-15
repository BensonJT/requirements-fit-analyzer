# db/

Holds `jobsearch.duckdb`, the single-file job store. The file itself is gitignored —
only this folder (and its .gitkeep) is tracked, so every checkout has the directory
ready to receive it.

Both the OptiPlex and Vostro checkouts should point at the **same** file on the author's
external drive (not two independent copies) — see
`Professional/Areas/Job_Search/Tools/DESIGN_ats_registry.md` §12a in the vault for why,
and for the upsert/lifecycle schema this file holds once Phase 1 lands.
