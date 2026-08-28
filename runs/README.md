# Published runs

This directory is intentionally tracked. A new experiment may write here
directly with `--output-root runs`; the simulator creates exactly one immutable
`output_<run_id>/` directory and refuses collisions.

Runs created elsewhere can be copied without transformation:

```bash
python tools/ingest_run.py /path/to/output_<run_id>
```

The ingest command validates and scans the source, copies every file byte for
byte, verifies SHA-256 equality, and never edits the run.
