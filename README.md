# magma-overlay (minimal)

Write a repo-local overlay spec listing `.spec` and `.m` files changed between git refs. Paths in the overlay are relative to the output file's directory.

```sh
MAGMA_USER_SPEC="$(pwd)/.magma_overlay.spec" magma
```

## Quickstart

1. Create `overlay.toml` (minimal):

```toml
# dev repo location (required)
repo_dir   = "/abs/path/to/your/repo"

# which refs to diff
baseline = "origin/main"
target   = "HEAD"

# whether to 'git fetch --prune' before diffing
fetch = false

[selectors]
# include specific commits (must be ancestors of 'target')
commits = [ ]
# include ranges A..B (B must be ancestor of 'target')
ranges  = [ ]
# force-include these paths relative to repo root
paths   = [ ]

[options]
restrict_prefixes = ["package/"]  # only consider files under these prefixes
order = "spec-first"              # or "lexicographic"

# optional extras
include_specs = [ ]        # prepend these specs (abs or repo-relative)
output = ".magma_overlay.spec"  # where to write the spec (default shown)
```

1. Generate the overlay spec in your repo:

```sh
python3 overlay/gen_patch.py overlay.toml
# writes <repo_dir>/.magma_overlay.spec
```

1. Run Magma with your overlay:

```sh
MAGMA_USER_SPEC="$(git rev-parse --show-toplevel)/.magma_overlay.spec" magma
```

Now launch Magma as usual; it will pick up `.magma_overlay.spec`.

## What it does

- Computes the union of changed files under `restrict_prefixes` from:
  - `git diff --name-status --diff-filter=AMR baseline..target`
  - per-commit first-parent diffs for entries in `selectors.commits`
  - `git diff --name-status --diff-filter=AMR` for each `selectors.ranges` A..B
  - plus every explicit path in `selectors.paths`
  - and, by default, uncommitted changes in the working tree (staged, unstaged, and untracked)
- Filters to existing files at `target`.
- Emits paths relative to the output file directory:
  - `+<SpecName.spec>` lines for `.spec` files
  - plain lines for `.m` files
- Optionally injects additional `+<relative spec>` lines from `include_specs` (resolved to absolute internally, then written relative to the output file directory).
- Keeps duplicates harmlessly (e.g., a file also included via a `+spec`).
- Writes the output atomically.

## Command

```text
python3 overlay/gen_patch.py <config.toml> [--output <path>]
```

- `<config.toml>`: TOML path (positional, required)
- `--output`: output spec file path (default `<repo_dir>/.magma_overlay.spec`); TOML `output` can set this too

Exit codes: 0 on success; nonzero on invalid config, git errors, or missing explicit paths.

## Config keys

- `repo_dir`: absolute or relative to the TOML location (required).
- `baseline` (default `origin/main`).
- `target` (default `HEAD` of `repo_dir`).
- `fetch` (default `false`; set `true` to update remotes before diffing).
- `selectors.commits`: list of commit hashes; each must be an ancestor of `target`.
- `selectors.ranges`: list of `A..B`; each `B` must be an ancestor of `target`.
- `selectors.paths`: repo-relative or absolute paths to force-include.
  - Paths outside `restrict_prefixes` are ignored unless explicitly listed.
  - Missing explicit paths are a hard error.
- `options.restrict_prefixes`: list of directory prefixes to consider, default `["package/"]`.
- `options.order`: `spec-first` or `lexicographic`.
- `options.baseline_mode`: how to interpret `baseline` versus `target`:
  - `raw` (default): use `baseline` as-is
  - `merge-base`: use `git merge-base baseline target`
  - `fork-point`: prefer `git merge-base --fork-point baseline target`, falling back to `merge-base` if unavailable
- `options.output_format`: how to write the overlay spec file:
  - `curly` (default): group by relative directory with curly-brace blocks; inside blocks, files are basenames and `+<SpecName.spec>` lists each `.spec` in that directory. If multiple directories share a common non-empty prefix, a single top-level `<common>/` block is used with nested subdirectory blocks. The whole file is wrapped in a top-level `{ ... }` block.
  - `flat`: relative `+<spec>` and `.m` paths, one per line; still wrapped in a top-level `{ ... }` block
- `options.include_uncommitted` (default `true`): include uncommitted changes (staged, unstaged, untracked). Set to `false` to disable.
- `include_specs`: extra spec files to include at top of output.
- `output`: output spec file path; if relative, resolved against `repo_dir`.

## Examples

Track only changes between `origin/main` and current branch:

```toml
repo_dir   = "/abs/path/to/your/repo"
baseline = "origin/main"
target = "HEAD"
# [options]
# baseline_mode = "raw"  # default
# output_format = "flat"   # optional
output = ".magma_overlay.spec"   # optional; default if omitted

[selectors]
commits = []
ranges = []
paths = []
```

Disable uncommitted changes (use only committed diffs and explicit selectors):

```toml
repo_dir   = "/abs/path/to/your/repo"
baseline = "origin/main"
target = "HEAD"

[options]
include_uncommitted = false
```
Track only files changed on this branch since it forked from `upstream/main`:

```toml
repo_dir   = "/abs/path/to/your/repo"
baseline = "upstream/main"
target = "HEAD"

[options]
baseline_mode = "fork-point"
restrict_prefixes = ["package/"]
# output_format = "curly"  # optional: grouped format
```


Pin a specific bugfix commit and one explicit file, and include a shared team spec:

```toml
repo_dir   = "/abs/path/to/your/repo"

[selectors]
commits = ["abc1234"]
paths = [
  "package/Lattice/Lat/lll.m",
  "package/Geometry/CrvG2/CrvG2.spec",
]

include_specs = ["/opt/magma/shared/team_overlay.spec"]
output = ".magma_overlay.spec"
```

## Notes

- Only additions/modifications/renames (AMR) are considered. Deletions are ignored.
- Only `.spec` and `.m` are emitted; other extensions are ignored.
- All content is read from the `target` worktree. No file copies into `root`.
- Paths are relative to the output file directory.
- `.sig` files will be generated by Magma next to the sources in the repo; ensure your `.gitignore` handles them.

## Troubleshooting

- `commit XYZ is not an ancestor of target`: rebase or change `target`. No detached blob copying is performed.
- `explicitly selected path missing at target`: fix the path or the branch; this is a hard error.
- `git fetch failed`: set `fetch = false` if offline, or fix credentials.

## Limitations

- Single overlay spec file path per run (you choose with `--output`).
- No deletions. If you need to mask files, you must modify specs accordingly.
- Only Linux/macOS shells are targeted.
