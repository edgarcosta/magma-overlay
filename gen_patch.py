#!/usr/bin/env python3
"""
Generate an overlay spec from a git repo and a TOML config.

This tool writes a single overlay spec file (by default
`<repo_dir>/.magma_overlay.spec`). All paths in the output are
relative to the directory of the output file:

- `.spec` entries are prefixed with '+'
- `.m` entries are printed without '+'

Rules:
- Diff baseline..target under `restrict_prefixes` (AMR only).
- Add files from `selectors.commits` (first-parent diffs) and `selectors.ranges` (diffs).
- Add explicit `selectors.paths`.
- Keep only `.spec` and `.m` files.
- Hard-fail if any explicitly selected path does not exist at target.
- Write atomically to the chosen output path.
- Optionally include user-provided spec files by inserting `+<relative path>`
  entries near the top of the generated spec.

Python >= 3.11 preferred (uses tomllib). For 3.8-3.10 install 'tomli'.
"""

import argparse
import os
import sys
import subprocess
from pathlib import Path

try:
    import tomllib  # py311+
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore


def relpath_posix(p: str | Path, start: str | Path) -> str:
    """Return a POSIX-style relative path from start to p."""
    return Path(os.path.relpath(str(p), start=str(start))).as_posix()


def run(cmd, cwd=None, check=True, capture=True):
    """Run a subprocess command and return the CompletedProcess.

    Parameters
    - cmd: List[str] command and its arguments
    - cwd: Optional working directory
    - check: If True, raise CalledProcessError on non-zero exit
    - capture: If True, capture stdout/stderr as text
    """
    proc = subprocess.run(
        cmd,
        cwd=cwd,
        check=check,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        text=True,
    )
    return proc


def parse_name_status(text):
    """Parse output from 'git diff --name-status' or 'git diff-tree --name-status'.

    Returns a list of path strings using the "new" path for renames.
    Assumes caller already filtered statuses to AMR.
    """
    paths = []
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.rstrip("\n").split("\t")
        status = parts[0]
        if status.startswith("R"):  # R100, R097, etc.
            if len(parts) < 3:
                continue
            new = parts[2]
            paths.append(new)
        else:
            if len(parts) < 2:
                continue
            p = parts[1]
            paths.append(p)
    return paths


def is_under_prefixes(path_rel, prefixes):
    """Return True if the relative path starts with any of the allowed prefixes."""
    return any(path_rel.startswith(pref) for pref in prefixes)


def parse_args():
    """Parse CLI arguments and return argparse.Namespace.

    Simplified interface:
    - <config.toml>: path to TOML (positional, required)
    - --output: path to write the generated spec (default: <repo_dir>/.magma_overlay.spec)
    """
    ap = argparse.ArgumentParser()
    ap.add_argument("config", help="Path to overlay.toml")
    ap.add_argument("--output", default=None, help="Output spec path. Default: <repo_dir>/.magma_overlay.spec")
    return ap.parse_args()


def load_config(cfg_path):
    """Load a TOML configuration file and return a dict."""
    with cfg_path.open("rb") as f:
        return tomllib.load(f)


def resolve_repo_dir_from_config(cfg, cfg_path: Path) -> Path:
    """Resolve the repository directory from config as an absolute Path."""
    if "repo_dir" not in cfg:
        print("ERROR: config must define 'repo_dir' (absolute or relative to the TOML).", file=sys.stderr)
        sys.exit(2)

    rd_raw = cfg["repo_dir"]
    rd_path = Path(rd_raw).expanduser()

    if rd_path.is_absolute():
        return rd_path.resolve()
    return (cfg_path.parent / rd_path).resolve()


def maybe_fetch(repo_dir, do_fetch: bool):
    """Run 'git fetch --prune' if do_fetch is True."""
    if not do_fetch:
        return
    try:
        run(["git", "-C", str(repo_dir), "fetch", "--prune"])
    except subprocess.CalledProcessError as e:
        print(f"ERROR: git fetch failed:\n{e.stderr}", file=sys.stderr)
        sys.exit(2)


def ensure_target_exists(repo_dir, target):
    """Ensure the git ref exists locally; exit on failure."""
    try:
        run(["git", "-C", str(repo_dir), "rev-parse", "--verify", target])
    except subprocess.CalledProcessError as e:
        print(f"ERROR: target ref invalid: {target}\n{e.stderr}", file=sys.stderr)
        sys.exit(2)


def ensure_selectors_are_ancestors(repo_dir, commits, ranges, target):
    """Ensure selected commits/ranges end at ancestors of the target; exit on failure."""
    for c in commits:
        try:
            run(["git", "-C", str(repo_dir), "merge-base", "--is-ancestor", c, target])
        except subprocess.CalledProcessError:
            print(f"ERROR: commit {c} is not an ancestor of {target}. Cannot include without copying.", file=sys.stderr)
            sys.exit(2)
    for rng in ranges:
        if ".." not in rng:
            print(f"ERROR: range must be A..B, got: {rng}", file=sys.stderr)
            sys.exit(2)
        A, B = rng.split("..", 1)
        try:
            run(["git", "-C", str(repo_dir), "merge-base", "--is-ancestor", B, target])
        except subprocess.CalledProcessError:
            print(f"ERROR: range tip {B} is not an ancestor of {target}. Cannot include without copying.", file=sys.stderr)
            sys.exit(2)


def collect_paths_from_diff(repo_dir, baseline, target, prefixes):
    """Collect AMR paths from diff of baseline..target filtered by prefixes."""
    rel_paths = set()
    try:
        p = run([
            "git", "-C", str(repo_dir), "diff", "--name-status",
            "--diff-filter=AMR", f"{baseline}..{target}"
        ])
        for rel in parse_name_status(p.stdout):
            if is_under_prefixes(rel, prefixes):
                rel_paths.add(rel)
    except subprocess.CalledProcessError as e:
        print(f"ERROR: diff baseline..target failed:\n{e.stderr}", file=sys.stderr)
        sys.exit(2)
    return rel_paths


def collect_paths_from_commits(repo_dir, commits, prefixes):
    """Collect AMR paths from first-parent diffs of individual commits."""
    rel_paths = set()
    for c in commits:
        try:
            p = run([
                "git", "-C", str(repo_dir), "diff-tree", "--name-status",
                "--first-parent", "-r", f"{c}^!", "--diff-filter=AMR"
            ])
            for rel in parse_name_status(p.stdout):
                if is_under_prefixes(rel, prefixes):
                    rel_paths.add(rel)
        except subprocess.CalledProcessError as e:
            print(f"ERROR: diff-tree for {c} failed:\n{e.stderr}", file=sys.stderr)
            sys.exit(2)
    return rel_paths


def collect_paths_from_ranges(repo_dir, ranges, prefixes):
    """Collect AMR paths from explicit A..B ranges."""
    rel_paths = set()
    for rng in ranges:
        A, B = rng.split("..", 1)
        try:
            p = run([
                "git", "-C", str(repo_dir), "diff", "--name-status",
                "--diff-filter=AMR", f"{A}..{B}"
            ])
            for rel in parse_name_status(p.stdout):
                if is_under_prefixes(rel, prefixes):
                    rel_paths.add(rel)
        except subprocess.CalledProcessError as e:
            print(f"ERROR: diff for range {rng} failed:\n{e.stderr}", file=sys.stderr)
            sys.exit(2)
    return rel_paths


def collect_paths_from_uncommitted(repo_dir: Path, prefixes):
    """Collect AMR paths from uncommitted changes in the working tree.

    Includes:
    - Staged changes: git diff --cached --name-status --diff-filter=AMR
    - Unstaged changes: git diff --name-status --diff-filter=AMR
    - Untracked files: git ls-files --others --exclude-standard
    """
    rel_paths = set()

    # Staged changes
    try:
        p = run([
            "git", "-C", str(repo_dir), "diff", "--cached", "--name-status", "--diff-filter=AMR"
        ])
        for rel in parse_name_status(p.stdout):
            if is_under_prefixes(rel, prefixes):
                rel_paths.add(rel)
    except subprocess.CalledProcessError as e:
        print(f"ERROR: diff --cached failed:\n{e.stderr}", file=sys.stderr)
        sys.exit(2)

    # Unstaged changes
    try:
        p = run([
            "git", "-C", str(repo_dir), "diff", "--name-status", "--diff-filter=AMR"
        ])
        for rel in parse_name_status(p.stdout):
            if is_under_prefixes(rel, prefixes):
                rel_paths.add(rel)
    except subprocess.CalledProcessError as e:
        print(f"ERROR: diff (worktree) failed:\n{e.stderr}", file=sys.stderr)
        sys.exit(2)

    # Untracked files (treat as added)
    try:
        p = run([
            "git", "-C", str(repo_dir), "ls-files", "--others", "--exclude-standard"
        ])
        for line in p.stdout.splitlines():
            rel = line.strip()
            if not rel:
                continue
            if is_under_prefixes(rel, prefixes):
                rel_paths.add(rel)
    except subprocess.CalledProcessError as e:
        print(f"ERROR: ls-files for untracked failed:\n{e.stderr}", file=sys.stderr)
        sys.exit(2)

    return rel_paths


def normalize_and_apply_explicit_paths(explicit_paths, repo_dir, prefixes):
    """Normalize explicit paths and include those under prefixes into the relative path set.

    Returns (explicit_set, rel_paths_from_explicit).
    """
    explicit_set = set()
    rel_paths = set()
    for pth in explicit_paths:
        p_expanded = Path(pth).expanduser()
        if p_expanded.is_absolute():
            # Allow absolute, but require it to be inside repo_dir
            try:
                abs_norm = p_expanded.resolve()
            except Exception:
                print(f"ERROR: invalid explicit path: {pth}", file=sys.stderr)
                sys.exit(2)
            try:
                rel = abs_norm.relative_to(repo_dir)
            except ValueError:
                print(f"ERROR: explicit path not under repo_dir: {pth}", file=sys.stderr)
                sys.exit(2)
            rel_s = rel.as_posix()
        else:
            rel_s = Path(pth).as_posix()
        explicit_set.add(rel_s)
        if is_under_prefixes(rel_s, prefixes):
            rel_paths.add(rel_s)
    return explicit_set, rel_paths


def materialize_and_classify(repo_dir, rel_paths, explicit_set):
    """Resolve relative paths to absolute and classify into .spec and .m.

    Also detect missing explicitly requested paths and non-explicit paths
    that vanished at the target.
    Returns (abs_spec, abs_m, missing_explicit, dropped_missing).
    """
    abs_spec = set()
    abs_m = set()
    missing_explicit = []
    dropped_missing = []

    for rel in sorted(rel_paths):
        abs_p = (repo_dir / rel).resolve()
        if not abs_p.exists():
            if rel in explicit_set:
                missing_explicit.append(rel)
            else:
                dropped_missing.append(rel)
            continue
        if rel.endswith(".spec"):
            abs_spec.add((repo_dir / rel).as_posix())
        elif rel.endswith(".m"):
            abs_m.add((repo_dir / rel).as_posix())
        else:
            # ignore other types
            pass

    return abs_spec, abs_m, missing_explicit, dropped_missing


def resolve_effective_baseline(repo_dir: Path, baseline_ref: str, target_ref: str, mode: str) -> str:
    """Resolve the effective baseline ref according to the chosen mode.

    Modes:
    - "raw": use baseline_ref as-is
    - "merge-base": use `git merge-base baseline_ref target_ref`
    - "fork-point": try `git merge-base --fork-point baseline_ref target_ref`,
      falling back to `git merge-base baseline_ref target_ref` if fork-point fails
    """
    mode_lc = (mode or "raw").lower()
    if mode_lc == "raw":
        return baseline_ref
    if mode_lc == "merge-base":
        try:
            p = run(["git", "-C", str(repo_dir), "merge-base", baseline_ref, target_ref])
            return p.stdout.strip()
        except subprocess.CalledProcessError as e:
            print(f"ERROR: git merge-base failed for {baseline_ref} and {target_ref}:\n{e.stderr}", file=sys.stderr)
            sys.exit(2)
    if mode_lc == "fork-point":
        # Try fork-point first
        try:
            p = run(["git", "-C", str(repo_dir), "merge-base", "--fork-point", baseline_ref, target_ref])
            out = p.stdout.strip()
            if out:
                return out
        except subprocess.CalledProcessError:
            # fall through to plain merge-base
            pass
        try:
            p = run(["git", "-C", str(repo_dir), "merge-base", baseline_ref, target_ref])
            return p.stdout.strip()
        except subprocess.CalledProcessError as e:
            print(f"ERROR: git merge-base fallback failed for {baseline_ref} and {target_ref}:\n{e.stderr}", file=sys.stderr)
            sys.exit(2)
    print(f"ERROR: unknown baseline_mode '{mode}'. Use 'raw', 'merge-base', or 'fork-point'.", file=sys.stderr)
    sys.exit(2)


def build_output_lines_flat(abs_spec, abs_m, order_mode, out_dir: Path, include_specs_abs=None):
    """Return flat formatted lines using paths relative to out_dir."""
    def to_rel(p: str) -> str:
        return relpath_posix(p, out_dir.as_posix())

    lines = []
    if include_specs_abs:
        for p in include_specs_abs:
            lines.append("+" + to_rel(p))
    if order_mode == "spec-first":
        for p in sorted(abs_spec):
            lines.append("+" + to_rel(p))
        for p in sorted(abs_m):
            lines.append(to_rel(p))
    else:
        both = [(0, "+" + to_rel(p)) for p in abs_spec] + [(1, to_rel(p)) for p in abs_m]
        for _, line in sorted(both, key=lambda x: x[1]):
            lines.append(line)
    return lines


def build_output_lines_curly(abs_spec, abs_m, out_dir: Path, include_specs_abs=None):
    """Return curly-brace grouped lines by directory.

    Format:
    <REL_DIR>
    {
      <File1.m>
      <File2.m>
      +<SpecName.spec> # list all .spec files selected in this dir
    }

    Notes:
    - Directory headers are relative to out_dir; files inside are basenames.
    - All include_specs are emitted as top-level '+<relative>' before groups.
    """
    from collections import defaultdict

    lines = []
    if include_specs_abs:
        for p in include_specs_abs:
            rel = relpath_posix(p, out_dir.as_posix())
            lines.append("+" + rel)

    dir_to_files = defaultdict(list)
    # Track the basenames of .spec files per directory (not just a boolean)
    dir_to_spec_files = defaultdict(set)

    # Group .m files by parent directory relative to out_dir
    for abs_path in abs_m:
        rel = relpath_posix(abs_path, out_dir.as_posix())
        parent = str(Path(rel).parent.as_posix())
        name = Path(rel).name
        dir_to_files[parent].append(name)

    # Record .spec basenames per directory
    for abs_path in abs_spec:
        rel = relpath_posix(abs_path, out_dir.as_posix())
        parent = str(Path(rel).parent.as_posix())
        name = Path(rel).name
        dir_to_spec_files[parent].add(name)

    # Normalize empty parent ('.') to empty string
    norm_dirs = []
    for d in set(list(dir_to_files.keys()) + list(dir_to_spec_files.keys())):
        norm_dirs.append("" if d == "." else d)
    all_dirs = sorted(set(norm_dirs))

    # Try to find a common non-empty root across directories and use a grouped view
    common_dir = None
    nonempty_dirs = [d for d in all_dirs if d]
    if len(nonempty_dirs) > 1:
        try:
            candidate = os.path.commonpath(nonempty_dirs)
            if candidate and candidate != "." and all(
                d == candidate or d.startswith(candidate.rstrip("/") + "/") for d in nonempty_dirs
            ):
                common_dir = candidate.rstrip("/")
        except Exception:
            common_dir = None

    if common_dir:
        # Group everything under the common_dir header
        lines.append(common_dir + "/")
        lines.append("{")
        prefix = common_dir + "/"

        # Files/specs directly in the common_dir
        for fname in sorted(dir_to_files.get(common_dir, [])):
            lines.append(f"  {fname}")
        for spec_name in sorted(dir_to_spec_files.get(common_dir, [])):
            lines.append(f"  +{spec_name}")

        # Subdirectories under common_dir
        for d in all_dirs:
            if not d or d == common_dir:
                continue
            if not d.startswith(prefix):
                continue
            rel = d[len(prefix):].rstrip("/")
            lines.append(f"  {rel}/")
            lines.append("  {")
            for fname in sorted(dir_to_files.get(d, [])):
                lines.append(f"    {fname}")
            for spec_name in sorted(dir_to_spec_files.get(d, [])):
                lines.append(f"    +{spec_name}")
            lines.append("  }")
        lines.append("}")
    else:
        # Fallback: one block per directory (root entries listed directly)
        for d in all_dirs:
            if d:
                lines.append((d.rstrip("/") + "/"))
                lines.append("{")
                for fname in sorted(dir_to_files.get(d, [])):
                    lines.append(f"  {fname}")
                for spec_name in sorted(dir_to_spec_files.get(d, [])):
                    lines.append(f"  +{spec_name}")
                lines.append("}")
            else:
                for fname in sorted(dir_to_files.get(d, [])):
                    lines.append(f"  {fname}")
                for spec_name in sorted(dir_to_spec_files.get(d, [])):
                    lines.append(f"  +{spec_name}")

    return lines


def write_atomic_lines(out_path: Path, lines):
    """Write lines atomically to out_path."""
    tmp_path = out_path.parent / ("." + out_path.name + ".tmp")
    with tmp_path.open("w", encoding="utf-8", newline="\n") as f:
        for line in lines:
            f.write(line + "\n")
    tmp_path.replace(out_path)
    return out_path


def main():
    """Entry point: read config, collect diffs, and write a single overlay spec."""
    args = parse_args()

    # Resolve configuration file
    cfg_path = Path(args.config).resolve()
    if not cfg_path.is_file():
        print(f"ERROR: config not found: {cfg_path}", file=sys.stderr)
        sys.exit(2)

    # Load configuration and extract options
    cfg = load_config(cfg_path)
    repo_dir = resolve_repo_dir_from_config(cfg, cfg_path)
    baseline = cfg.get("baseline", "origin/main")
    target = cfg.get("target", "HEAD")
    do_fetch = bool(cfg.get("fetch", False))
    selectors = cfg.get("selectors", {})
    commits = selectors.get("commits", []) or []
    ranges = selectors.get("ranges", []) or []
    explicit_paths = selectors.get("paths", []) or []

    opts = cfg.get("options", {})
    prefixes = opts.get("restrict_prefixes", ["package/"])
    order_mode = opts.get("order", "spec-first")
    baseline_mode = opts.get("baseline_mode", "raw")
    output_format = opts.get("output_format", "curly")
    include_uncommitted = bool(opts.get("include_uncommitted", True))
    include_specs_from_cfg = cfg.get("include_specs", []) or []

    # Basic repo checks and optional fetch
    if not repo_dir.is_dir():
        print(f"ERROR: repo_dir does not exist: {repo_dir}", file=sys.stderr)
        sys.exit(2)
    maybe_fetch(repo_dir, do_fetch)
    ensure_target_exists(repo_dir, target)
    ensure_selectors_are_ancestors(repo_dir, commits, ranges, target)

    # Collect relative paths from diffs and selectors
    rel_paths = set()
    effective_baseline = resolve_effective_baseline(repo_dir, baseline, target, baseline_mode)
    rel_paths |= collect_paths_from_diff(repo_dir, effective_baseline, target, prefixes)
    rel_paths |= collect_paths_from_commits(repo_dir, commits, prefixes)
    rel_paths |= collect_paths_from_ranges(repo_dir, ranges, prefixes)
    if include_uncommitted:
        rel_paths |= collect_paths_from_uncommitted(repo_dir, prefixes)

    # Apply explicit path selections
    explicit_set, rel_from_explicit = normalize_and_apply_explicit_paths(explicit_paths, repo_dir, prefixes)
    rel_paths |= rel_from_explicit

    # Resolve and classify
    abs_spec, abs_m, missing_explicit, dropped_missing = materialize_and_classify(repo_dir, rel_paths, explicit_set)

    # Keep absolute, resolved paths for relative computation against output directory
    abs_spec = set(Path(p).resolve().as_posix() for p in abs_spec)
    abs_m = set(Path(p).resolve().as_posix() for p in abs_m)

    if missing_explicit:
        print("ERROR: explicitly selected paths missing at target:", file=sys.stderr)
        for rel in missing_explicit:
            print(f"  {rel}", file=sys.stderr)
        sys.exit(2)

    for rel in dropped_missing:
        print(f"WARNING: path missing at target and was dropped: {rel}", file=sys.stderr)

    # Resolve include specs to absolute, resolved paths (supports top-level or [options])
    include_specs_abs = []
    # Accept include_specs at top-level or within [options]
    include_specs_all = (cfg.get("include_specs", []) or []) or (opts.get("include_specs", []) or [])
    for sp in include_specs_all:
        p = Path(sp).expanduser()
        if not p.is_absolute():
            include_specs_abs.append((repo_dir / p).resolve().as_posix())
        else:
            include_specs_abs.append(p.resolve().as_posix())

    # Determine output path (CLI > TOML > default) so we can compute relative paths
    # Accept output at top-level or within [options]
    cfg_output = cfg.get("output") or opts.get("output")
    chosen_output = args.output if args.output else cfg_output
    if chosen_output:
        out_path = Path(chosen_output).expanduser()
        if not out_path.is_absolute():
            out_path = (repo_dir / out_path).resolve()
    else:
        out_path = (repo_dir / ".magma_overlay.spec").resolve()

    # Build output
    fmt = (output_format or "flat").lower()
    if fmt == "flat":
        lines = build_output_lines_flat(abs_spec, abs_m, order_mode, out_path.parent, include_specs_abs)
    elif fmt == "curly":
        lines = build_output_lines_curly(abs_spec, abs_m, out_path.parent, include_specs_abs)
    else:
        print(f"ERROR: unknown output_format '{output_format}'. Use 'flat' or 'curly'.", file=sys.stderr)
        sys.exit(2)

    # Always wrap with a top-level curly block
    lines = ["{"] + lines + ["}"]

    out_path = write_atomic_lines(out_path, lines)

    print(f"Wrote {len(abs_spec)} spec and {len(abs_m)} source entries to {out_path}")


if __name__ == "__main__":
    main()

