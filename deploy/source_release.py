"""Prepare reviewed Hermes source releases without importing or activating them."""
from __future__ import annotations

import argparse
import ast
import difflib
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import tempfile

COMMIT = re.compile(r"[0-9a-f]{40}")
SAFE_PATH = re.compile(r"[a-zA-Z0-9_./+-]+")
REF = "refs/heads/published"
SOURCE_EXTENSIONS = {".py", ".sh", ".bash", ".zsh", ".ts", ".tsx", ".js", ".mjs",
                     ".yaml", ".yml", ".toml", ".css", ".html", ".rs"}
SOURCE_NAMES = {"start-supervisor", "ov-client"}
NON_RUNTIME = {"README.md", "LICENSE", "COPYING", "NOTICE"}
EXCLUDED_PARTS = {"auth", "backups", "cache", "logs", "state", "sessions", "node_modules",
                  "venv", ".git", ".venv", "__pycache__"}
MAX_FILE = 5_000_000


class ReleaseError(RuntimeError):
    pass


def require(ok, message):
    if not ok:
        raise ReleaseError(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def encode(value):
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()


def atomic(path, data):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as f:
        tmp = Path(f.name)
        try:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)


def git(cwd, *args, allow=()):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0")
    r = subprocess.run(["git", "-C", str(cwd), "-c", "core.hooksPath=/dev/null",
                        "-c", "protocol.file.allow=never", *args], env=env,
                       capture_output=True, timeout=60, check=False)
    require(r.returncode == 0 or r.returncode in allow, f"git {args[0]} failed ({r.returncode})")
    return r


def relative(name):
    p = PurePosixPath(name)
    require(SAFE_PATH.fullmatch(name) and not p.is_absolute() and str(p) == name and
            not (set(p.parts) & (EXCLUDED_PARTS | {".."})) and
            not any(part.startswith(".") for part in p.parts),
            f"unmanaged source path: {name}")
    return p


def read(path):
    require(not path.is_symlink() and path.resolve() == path and path.is_file() and
            path.stat().st_size <= MAX_FILE, f"unsafe or absent source: {path.name}")
    return path.read_bytes()


class ProfileShape(ast.NodeTransformer):
    def visit_Constant(self, node):
        if isinstance(node.value, str):
            node.value = "<profile literal>"
        return node

    def visit_Tuple(self, node):
        if node.elts and all(isinstance(x, ast.Constant) and isinstance(x.value, str)
                             for x in node.elts):
            return ast.Constant(value="<profile string tuple>")
        return self.generic_visit(node)


def profile_only(name, public, private):
    if name.endswith(".py"):
        return ast.dump(ProfileShape().visit(ast.parse(public))) == ast.dump(
            ProfileShape().visit(ast.parse(private)))
    if name in {"runtime/hermes-home/plugins/ole-second-brain/plugin.yaml",
                "runtime/hermes-home/plugins/ole-workflow-memory/plugin.yaml"}:
        value = re.sub(r"\bOle\b", "Alex", private.decode())
        return re.sub(r"\bOles\b", "des Nutzers", value).encode() == public
    return False


def private_patch(rows):
    patch = []
    for name, public, private in rows:
        diff = difflib.unified_diff(public.decode().splitlines(keepends=True),
                                    private.decode().splitlines(keepends=True),
                                    "a/" + name, "b/" + name, n=2)
        for line in diff:
            patch.append(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n")
    return "".join(patch).encode()


class Release:
    def __init__(self, state, layout):
        self.state, self.layout = state, layout
        self.repo = state / "source.git"
        require(re.fullmatch(r"https://github\.com/[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+\.git",
                             layout["repository"]), "expected a GitHub HTTPS repository")
        require(COMMIT.fullmatch(layout["upstream"]), "expected pinned upstream commit")
        self.roots = {k: Path(v) for k, v in layout["roots"].items()}
        self.exact = {k: Path(v) for k, v in layout.get("files", {}).items()}
        for key, value in {**self.roots, **self.exact}.items():
            relative(key)
            require(value.is_absolute() and value.resolve() == value, "unsafe private source layout")
        self.core = self.roots["core"]

    def target(self, name):
        p = relative(name)
        if name in self.exact:
            return self.exact[name]
        for prefix in sorted(self.roots, key=len, reverse=True):
            if name.startswith(prefix + "/"):
                return self.roots[prefix].joinpath(*p.parts[len(PurePosixPath(prefix).parts):])
        raise ReleaseError(f"no native release binding for {name}")

    def fetch(self, commit):
        require(COMMIT.fullmatch(commit), "provide the full reviewed commit ID")
        if not self.repo.exists():
            git(self.state, "init", "--bare", self.repo.name)
        git(self.repo, "fetch", "--no-tags", "--no-recurse-submodules",
            self.layout["repository"], "refs/heads/main:" + REF)
        git(self.repo, "merge-base", "--is-ancestor", commit, REF)

    def build(self, commit, directory):
        git(self.core, "cat-file", "-e", self.layout["upstream"] + "^{commit}")
        files = {}
        for row in git(self.repo, "ls-tree", "-r", "-z", commit).stdout.split(b"\0"):
            if not row:
                continue
            meta, name = row.split(b"\t", 1)
            name = name.decode()
            if not name.startswith("runtime/") or PurePosixPath(name).name in NON_RUNTIME:
                continue
            p = relative(name)
            require(meta.split()[0] in (b"100644", b"100755") and
                    (p.suffix in SOURCE_EXTENSIONS or p.name in SOURCE_NAMES),
                    f"not a deployable source file: {name}")
            files[name] = git(self.repo, "show", commit + ":" + name).stdout
        core = directory / "core"
        core.mkdir()
        patch = directory / "upstream.patch"
        patch.write_bytes(git(self.repo, "show", commit + ":patches/hermes-agent.patch").stdout)
        for row in git(core, "apply", "--numstat", "-z", str(patch)).stdout.split(b"\0"):
            if not row:
                continue
            _, _, raw_name = row.split(b"\t", 2)
            name = raw_name.decode()
            p = relative(name)
            require(p.suffix in SOURCE_EXTENSIONS, "unmanaged core patch file")
            files["core/" + name] = None
            b = git(self.core, "show", self.layout["upstream"] + ":" + name, allow=(128,))
            if b.returncode == 0:
                dst = core / name
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(b.stdout)
        git(core, "apply", "--check", str(patch))
        git(core, "apply", str(patch))
        patch.unlink()
        for name, body in files.items():
            dest = directory / name
            if body is not None:
                require(len(body) <= MAX_FILE, f"oversized source: {name}")
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(body)
            else:
                require(dest.is_file(), f"core deletion needs its own reviewed migration: {name}")
        return sorted(files)

    def render(self, directory, names):
        patch = self.state / "profile.patch"
        if patch.stat().st_size:
            git(directory, "apply", "--check", str(patch))
            git(directory, "apply", str(patch))
        result = {}
        for name in names:
            p = directory / name
            body = read(p)
            if p.suffix == ".py":
                compile(body, name, "exec")
            result[name] = digest(body)
        return result

    def live(self, names, *, allow_absent=False):
        result = {}
        for name in names:
            p = self.target(name)
            require(p.resolve() == p, f"symbolic link at live source: {name}")
            if allow_absent and not p.exists():
                result[name] = None
            else:
                result[name] = digest(read(p))
        return result

    def active(self):
        return json.loads(read(self.state / "active.json"))

    def enroll(self, commit, expected_private):
        self.fetch(commit)
        with tempfile.TemporaryDirectory(dir=self.state, prefix="verify-") as temp:
            directory = Path(temp)
            names = self.build(commit, directory)
            rows = []
            for name in names:
                public, private = read(directory / name), read(self.target(name))
                if public != private:
                    require(profile_only(name, public, private), f"live logic differs: {name}")
                    rows.append((name, public, private))
            require(len(rows) == expected_private, "unexpected count of private differences")
            atomic(self.state / "profile.patch", private_patch(rows))
            expected = self.render(directory, names)
            require(expected == self.live(names), "private profile did not reconstruct running source")
            atomic(self.state / "active.json", encode({"commit": commit, "files": expected,
                  "profile": digest(read(self.state / "profile.patch"))}))
        return {"status": "adopted", "commit": commit, "managed_files": len(names),
                "private_files": len(rows), "production_code_writes": 0}

    def status(self):
        active = self.active()
        current = self.live(active["files"], allow_absent=True)
        drift = sorted(n for n, sha in active["files"].items() if current[n] != sha)
        return {"commit": active["commit"], "managed_files": len(current), "drift": drift,
                "profile_matches": active["profile"] == digest(read(self.state / "profile.patch"))}

    def prepare(self, commit):
        self.fetch(commit)
        active = self.active()
        git(self.repo, "merge-base", "--is-ancestor", active["commit"], commit)
        s = self.status()
        require(not s["drift"] and s["profile_matches"], "installed source or profile drift; review status")
        stage = self.state / "staged" / commit
        stage.parent.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=self.state, prefix="prepare-") as temp:
            tree = Path(temp) / "payload"
            tree.mkdir()
            names = self.build(commit, tree)
            require(set(active["files"]) <= set(names), "source removal requires reviewed retirement")
            expected = self.render(tree, names)
            before = self.live(names, allow_absent=True)
            require(all(before[n] == sha for n, sha in active["files"].items()),
                    "installed source changed during staging")
            require(len({self.target(n) for n in names}) == len(names), "duplicate live source binding")
            require(all(before[n] is None for n in set(names) - set(active["files"])),
                    "new source would overwrite an unmanaged file")
            changes = [{"source": n, "target": str(self.target(n)), "before": before[n],
                        "after": expected[n]} for n in names if expected[n] != before[n]]
            plan = {"commit": commit, "previous": active["commit"], "profile": active["profile"],
                    "files": expected, "changes": changes}
            if not stage.exists():
                bundle = Path(temp) / "bundle"
                bundle.mkdir()
                tree.rename(bundle / "payload")
                atomic(bundle / "plan.json", encode(plan))
                bundle.rename(stage)
            else:
                require(encode(plan) == read(stage / "plan.json") and
                        all(digest(read(stage / "payload" / n)) == sha for n, sha in expected.items()),
                        "existing stage differs; reconcile before reuse")
        return {"status": "prepared", "commit": commit, "managed_files": len(names),
                "changes": len(changes), "plan": str(stage / "plan.json")}

    def verify(self, commit, publisher_inputs=None, *, accept=False):
        require(COMMIT.fullmatch(commit), "provide the full reviewed commit ID")
        stage = self.state / "staged" / commit
        plan = json.loads(read(stage / "plan.json"))
        active = self.active()
        require(plan["commit"] == commit and plan["previous"] == active["commit"] and
                plan["profile"] == active["profile"] == digest(read(self.state / "profile.patch")),
                "stale stage or private profile")
        with tempfile.TemporaryDirectory(dir=self.state, prefix="readback-") as temp:
            tree = Path(temp)
            names = self.build(commit, tree)
            expected = self.render(tree, names)
        require(expected == plan["files"] and all(
            digest(read(stage / "payload" / n)) == sha for n, sha in expected.items()),
            "prepared source differs from the Git release")
        current = self.live(expected, allow_absent=True)
        if accept:
            require(current == expected, "installed source does not match the prepared release")
            atomic(self.state / "active.json", encode({"commit": commit, "files": expected,
                                                       "profile": plan["profile"]}))
            return {"status": "accepted", "commit": commit, "managed_files": len(expected)}
        prior = {n: active["files"].get(n) for n in names}
        require(current == prior, "source changed after staging")
        changes = {str(self.target(n)): (n, sha) for n, sha in expected.items() if current[n] != sha}
        require(plan["changes"] == [{"source": n, "target": target, "before": current[n],
                                     "after": sha} for target, (n, sha) in changes.items()],
                "publication plan changed")
        if publisher_inputs:
            rows = json.loads(read(publisher_inputs))
            require(len(rows) == len(changes) and {r["target"] for r in rows} == set(changes),
                    "native publisher has missing or extra source targets")
            for row in rows:
                _, sha = changes[row["target"]]
                require(digest(read(Path(row["source"]))) == sha, "native publisher source changed")
        return {"status": "verified", "commit": commit, "changes": len(changes),
                "native_inputs_verified": publisher_inputs is not None}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--state", type=Path, required=True)
    sub = p.add_subparsers(dest="action", required=True)
    new = sub.add_parser("enroll")
    new.add_argument("commit")
    new.add_argument("--layout", type=Path, required=True)
    new.add_argument("--private-differences", type=int, required=True)
    sub.add_parser("status")
    for action in ("prepare", "verify", "accept"):
        parser = sub.add_parser(action)
        parser.add_argument("commit")
        if action == "verify":
            parser.add_argument("--native-inputs", type=Path)
    a = p.parse_args()
    state = a.state.absolute()
    require(state.resolve() == state, "symbolic link at release state")
    os.umask(0o077)
    if a.action == "enroll":
        require(not state.exists(), "release state already exists")
        with tempfile.TemporaryDirectory(dir=state.parent, prefix=".source-enroll-") as temp:
            root = Path(temp) / "release"
            root.mkdir()
            layout = json.loads(read(a.layout.absolute()))
            atomic(root / "layout.json", encode(layout))
            result = Release(root, layout).enroll(a.commit, a.private_differences)
            root.rename(state)
    else:
        require(state.is_dir() and state.stat().st_uid == os.geteuid() and
                state.stat().st_mode & 0o077 == 0, "release state must be private and owned")
        with (state / "release.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            release = Release(state, json.loads(read(state / "layout.json")))
            if a.action == "status":
                result = release.status()
            elif a.action == "prepare":
                result = release.prepare(a.commit)
            else:
                result = release.verify(a.commit, getattr(a, "native_inputs", None),
                                        accept=a.action == "accept")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except (ReleaseError, BlockingIOError) as error:
        raise SystemExit(str(error))
