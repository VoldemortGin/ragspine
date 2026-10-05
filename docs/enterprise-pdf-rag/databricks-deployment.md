# Databricks deployment conditions

This document separates the installable Python package contract from a real
Databricks deployment. The target Databricks workload type has not been chosen.
Do not infer that the repository has been deployed to, or validated inside, a
Databricks workspace.

## Package contract

Local development continues to use uv and the committed lock file. A deployment
environment must use Python 3.12 and be able to install the backend with one
standard command:

```sh
python -m pip install .
```

Standard pip resolves the version constraints in `pyproject.toml`; it does not
read `uv.lock`. The command therefore proves that the declared runtime set is
installable, but it does not promise the same transitive versions as the local uv
environment. A separately governed constraints artifact can be added later if a
production environment requires pip-level transitive locking.

That command must install the PDF and SVG runtime needed by the shipped backend,
including the sole PDF entry point, pdfspine. Development-only tools remain out
of the default runtime dependency set. A valid package check builds a wheel,
installs it into a clean Python 3.12 environment, runs `pip check`, and exercises
package imports, the packaged reviewed-region resource, a small SVG render, and
API construction without parsing a real PDF or calling a model. This local check
proves package completeness; it does not prove Databricks networking, storage,
permissions, or process compatibility.

Runtime data is external to the wheel. `APP_DATA_DIR` selects the writable data
root for source documents, manifests, intermediate artifacts, vectors, and
published pointers. `APP_ROOT_DIR` selects an explicit configuration or working
directory when needed. Neither variable may cause a fallback to bundled sample
data or synthetic business results.

The one-command pip contract covers the backend, including its API, ingestion,
SVG processing, and retrieval adapters. It deliberately does not install Open
WebUI. Open WebUI remains a separately versioned upstream application with a
much larger dependency surface; combining it with the backend requires a
separate compatibility decision and installation check.

## Installed backend startup

After installing the project into a Python 3.12 environment, start the packaged
API without uv or a source-checkout `.venv`:

```sh
export APP_ROOT_DIR=/absolute/path/to/existing-work-directory
export APP_DATA_DIR=/absolute/path/to/persistent-data
export APP_EXECUTION_MODE=aia-source-review
enterprise-pdf-rag serve --host 127.0.0.1 --port 8766
```

`APP_DATA_DIR` must already contain the immutable source and processing closure,
including `output/aia-2026-interim/current-manifest` and
`output/aia-2026-interim/pages-001-020/current-processing`. Startup validates
and serves that state; it does not ingest a PDF, build embeddings, or call a
model. Relative `APP_DATA_DIR` values resolve against `APP_ROOT_DIR`, rather than
the caller's current directory.

Use a platform-provided host and port when required. For example, a future
Databricks App entry point would bind to `0.0.0.0` and the value of
`DATABRICKS_APP_PORT`; the literal loopback address and port above are a portable
backend example, not an `app.yaml` decision.

## Notebook and Job condition

The Python version for a notebook or Job comes from its selected Databricks
Runtime or serverless environment; it is not a platform-wide constant. Two
current official examples that meet this project's Python requirement are:

- Databricks Runtime 17.3 LTS, whose documented system environment uses Python
  3.12.3.
- Serverless environment versions 5 and 6, whose documented system environments
  use Python 3.12.3.

Serverless notebook and Job dependencies can reference a Python project (a
directory containing `pyproject.toml`) or a wheel in workspace files or a Unity
Catalog volume. Notebook-scoped `%pip` installs are session-scoped and must be
reinstalled for a new session. Job tasks use isolated environments. A concrete
deployment must therefore pin an eligible Python 3.12 environment and install
the project or its wheel as a declared dependency; merely running successfully
in the local uv environment is insufficient.

Official references:

- [Configure the serverless environment](https://docs.databricks.com/aws/en/compute/serverless/dependencies)
- [Serverless environment versions](https://docs.databricks.com/aws/en/release-notes/serverless/environment-version)
- [Databricks Runtime 17.3 LTS](https://docs.databricks.com/aws/en/release-notes/runtime/17.3lts)
- [Notebook-scoped Python libraries](https://docs.databricks.com/aws/en/libraries/notebooks-python-libraries)
- [Install libraries](https://docs.databricks.com/aws/en/libraries)

## Databricks Apps condition

Databricks Apps has two distinct Python installation modes:

- If `requirements.txt` exists, Databricks uses pip and that file takes
  precedence over `pyproject.toml`. The documented pip-based Apps environment
  uses Python 3.11, so it does not satisfy this project's
  `requires-python = ">=3.12,<3.13"` contract.
- If `requirements.txt` is absent and both `pyproject.toml` and `uv.lock` are
  present, Databricks uses uv. In this mode, `requires-python` can select a
  Python version other than the default, including this project's Python 3.12
  range, and Databricks creates the virtual environment.

Consequently, choosing Apps would require the documented uv deployment path for
this project unless the Python contract is deliberately changed. This repository
does not add `requirements.txt` or `app.yaml` until the workload type is chosen.

Apps also impose runtime constraints that the current local launcher does not
model:

- `app.yaml` provides one command, and the serving application must bind to
  `0.0.0.0` on `DATABRICKS_APP_PORT`. The local API/WebUI pair on ports 8766 and
  8767 cannot be declared as two public app ports without a Databricks-specific
  ingress design.
- App memory and local filesystem state are temporary and are lost on restart.
  Persistent unstructured artifacts belong in workspace files or a Unity Catalog
  volume; structured state can use Databricks tables or another declared
  persistent resource.
- Apps run without elevated privileges and cannot install system packages with
  `apt-get`, `yum`, or `apk`. Every required native component must therefore be
  available through a compatible Python wheel or an already provided system
  runtime.
- Apps must handle `SIGTERM` within 15 seconds, write operational logs to stdout
  or stderr, and avoid heavy installs or data processing during startup.
- Each file deployed with an app must be no larger than 10 MB. Source PDFs and
  generated document artifacts must remain outside the app source bundle.

The official documentation does not, in the cited pages, categorically prohibit
child processes. It does specify one managed command and one application port.
Any decision to supervise both the backend and Open WebUI inside one App therefore
requires a separate deployment design and an actual workspace test; it is not a
property established by `pip install .`.

Official references:

- [Manage dependencies for a Databricks app](https://docs.databricks.com/aws/en/dev-tools/databricks-apps/dependencies)
- [Databricks Apps environment](https://docs.databricks.com/aws/en/dev-tools/databricks-apps/system-env)
- [Configure app execution with `app.yaml`](https://docs.databricks.com/aws/en/dev-tools/databricks-apps/app-runtime)
- [Key concepts in Databricks Apps](https://docs.databricks.com/aws/en/dev-tools/databricks-apps/key-concepts)
- [Best practices for Databricks Apps](https://docs.databricks.com/aws/en/dev-tools/databricks-apps/best-practices)
- [Develop apps](https://docs.databricks.com/aws/en/dev-tools/databricks-apps/app-development)

## Data directory on workspace files or a Unity Catalog volume

`notebooks/run_folder.ipynb` writes everything under `ROOT_DIR/data`. Run from a Git folder,
that is a workspace-files path (`/Workspace/...`); a data root can also be a volume
(`/Volumes/...`). Both are FUSE mounts, and both refuse some operations a local disk allows.

What the official documentation states (checked 2026-10-03):

- Volumes: "Direct-append or non-sequential (random) writes are not supported. This affects
  operations like writing Zip and Excel files"; sparse files are not supported
  ([Work with files in Unity Catalog volumes](https://docs.databricks.com/aws/en/volumes/volume-files)).
- Workspace files: a file is limited to 500 MB; executors cannot write to workspace files;
  symlinks may only point under `/Workspace`; access to files under `/Workspace` expires after
  36 hours for interactive compute and 30 days for jobs
  ([What are workspace files?](https://docs.databricks.com/aws/en/files/workspace)).
  Files can be created, edited, renamed and deleted programmatically on Databricks Runtime
  11.3 LTS and above, the parent directory must already exist, and writes are buffered and
  flushed asynchronously, so an error can surface after the write call has returned
  ([Programmatically interact with workspace files](https://docs.databricks.com/aws/en/files/workspace-interact)).
- Git folders: a working branch is limited to 1 GB, Databricks recommends fewer than 20,000
  workspace assets and files in total, and temporary files belong in `$TEMPDIR` rather than the
  workspace filesystem ([Git folder limits](https://docs.databricks.com/aws/en/repos/limits)).

The documentation does not say whether hard links, `O_CREAT | O_EXCL`, rename over an existing
file, file fsync or directory fsync work on either mount. Observed on one user's Databricks
data directory (which of the two mounts was not recorded): `os.link` fails with `EPERM`, while
creating, writing and fsyncing a new file in the same directory succeeds.

How the backend adapts ([ADR 0020](adr/0020-storage-without-hard-links.md)):

- The source / processing stores and the model cache publish a finished temporary file with a
  hard link. Where that fails with "not supported" (`EPERM`, `ENOTSUP`, `ENOSYS`, `EXDEV`) they
  check that the name is absent, rename the temporary into place and read it back. Readers still
  only see complete files. What becomes weaker: first-writer-wins is decided by a check followed
  by a rename, not atomically, so two processes writing *different* bytes under one name at the
  same moment are detected on one side only. Run one pipeline per ingestion directory at a time.
- The directory fsync after a model-call claim is skipped when the mount answers `EINVAL`,
  `EPERM`, `ENOTSUP` or `ENOSYS`; a claim may then not survive a crash of the node.
- A claim left by an interrupted or killed kernel no longer blocks its call forever
  ([ADR 0023](adr/0023-claim-takeover.md)): it is taken over at once when its process on the
  same host is gone, otherwise after its lease (840 s for the 180 s ingestion / tree clients);
  a claim written by the previous code (no holder recorded) after 15 minutes by its mtime. The
  takeover is an `O_EXCL` create (no hard link, no rename). Claims are deleted once their record
  is written.
- All writes on the pipeline are sequential, whole-file writes. The notebook writes
  `answers.csv` in one pass through a `.partial` sibling and a rename instead of appending.
- The answer journal `answers-audit.sqlite` uses SQLite in WAL mode, which needs random writes
  and shared-memory locking. Where the mount refuses them the journal is disabled with a logged
  warning and answering continues; the answer text in `answers.csv` comes from the response
  itself, not from the journal. Neither ingestion nor answering depends on it.

Performance on Workspace files: every file operation (open, read, write, fsync, rename, stat)
is a round trip, so the pipeline's cost is roughly its number of file operations times that
latency, almost independent of how many bytes move. Before [ADR
0024](adr/0024-source-verification-cache.md) the stores re-read and re-hashed the whole source
snapshot (the PDF plus every page's SVG and text) once per indexable object in several stages,
so file operations grew with pages × objects. One reported run on a few-hundred-page encrypted
report was still ingesting after eight hours. Now each store instance verifies a snapshot once,
and file operations grow linearly. A synthetic local harness (8 text objects a page, no hard
links) counts about 2 900 file operations per page on a first run and about 860 per page on a
fully cached rerun, against 6 400 and 3 700 per page at 30 pages before and growing. At an
assumed 20–50 ms per operation, a 300-page document would take roughly 5–12 hours on a first
run and 1.5–3.5 hours on a rerun; before the change, those figures were roughly 48–120 and
29–72 hours. These are extrapolations, not Databricks measurements. Most of what remains on a
first run is writing the content-addressed objects through the ADR 0020 fallback. Keep the
ingestion directory on the fastest mount available. A rerun whose model replies are all cached
still verifies, re-derives and re-exports every object once.

Not validated on Databricks: rename and exclusive creation on these mounts, SQLite behavior,
and performance with the many small
content-addressed files one PDF produces (hundreds to thousands, which counts against the
20,000-file recommendation in a Git folder). The fallback was exercised against simulated
failures and on a local exFAT volume, which also cannot hard-link.

## Work deferred until the target is known

The following items require the actual Databricks workload type and workspace
configuration. They are not part of the portable pip-install contract:

- `app.yaml`, a requirements file, a Databricks Asset Bundle, or a Job task
  definition;
- the public ingress layout for the API and Open WebUI;
- the Unity Catalog volume or workspace-file paths, permissions, and migration
  of the current local data tree;
- Databricks secret/resource bindings and egress policy;
- compute sizing, lifecycle behavior, observability, and an end-to-end deployment
  test in the selected workspace.

## Validation status

An isolated local Python 3.12 environment successfully installed the project with
`python -m pip install .`, and `pip check` reported no broken requirements. The
result contained 23 default runtime distributions, including pdfspine 0.10.0 and
resvg-py 0.5.0, and excluded the development tools and Open WebUI. The built wheel
contained the reviewed-region catalog and `py.typed`; it excluded runtime data,
scripts, and tests.

A separate static artifact check found published x86-64 manylinux wheels for the
pinned pdfspine and resvg-py versions and inspected their ELF shared-library
requirements. This is evidence that the selected native packages have Linux
wheels; it is not an execution test in a Databricks image and does not prove that
every future target image supplies the required base libraries.

The installed package was non-editable and ran from a temporary working
directory without the source tree or project marker. With copied external data,
the packaged console server returned HTTP 200 from `/v1/models` and
`/v1/processing/status`; the latter reported the expected 20-page, 241-IR,
241-description snapshot. Packaged catalog access, a 10-by-10 SVG render, and PDF
adapter import also passed. This smoke test performed no PDF parsing and made no
model call. The complete offline gate then passed 363 tests, formatting and Ruff
checks across 159 files, strict mypy across 141 source files, architecture checks,
four schema checks, and drift checks.

No Databricks workspace deployment, Databricks ingress, persistent-volume mount,
or combined backend/Open WebUI environment has been exercised yet. Those remain
deployment acceptance tests after the target workload type and workspace are
known.
