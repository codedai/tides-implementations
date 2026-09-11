# Offline source data

This directory is the immutable input boundary for the TTC prototype. The pipeline may read these files but must not edit, rename, reformat, or delete them.

Expected layout:

```text
database_offline/
├── gtfs/    # Versioned GTFS ZIP archives
├── avl/     # Source AVL CSV exports
└── apc/     # Source APC CSV exports
```

CSV and ZIP files under this directory are ignored by Git because the current fixture is about 1.3 GB and its redistribution status has not been established. Each run should calculate content hashes and record them in generated run metadata so results remain reproducible without committing the data.

Do not place intermediate or output data here. Generated data belongs under [`../artifacts/`](../artifacts/README.md).
