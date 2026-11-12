"""What a set of sessions depends on: repos, tools and credentials.

All of it is read-only and reports names, paths and sizes. It never prints a
credential's value: a credential is reported by where it lives and what kind
of thing it is, and the user decides per item whether it travels.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable, Optional

# Directories that are rebuilt, not moved: left out of every copy (trees,
# untracked files, memory, skills) and never counted toward a size. A
# virtualenv or node_modules is built for this machine's OS and would not run
# on the agent anyway.
SKIP_DIRS = {
    ".git", "node_modules", ".venv", "venv", "__pycache__", ".mypy_cache", ".pytest_cache",
    ".ruff_cache", ".tox", ".next", "dist", "build", "target", ".gradle", ".cache",
}

# Files the OS writes beside real ones: macOS Finder state and AppleDouble
# ("._name") files, which carry extended attributes on filesystems without them.
JUNK_FILE_RE = re.compile(r"^(\._.*|\.DS_Store)$")


def is_rebuilt_or_junk(rel: str, keep_git: bool = False) -> bool:
    """Whether a relative path is under a rebuilt directory (a virtualenv,
    node_modules, build output, ...) or is an OS junk file."""
    parts = Path(rel).parts
    if not parts:
        return False
    skip = SKIP_DIRS - {".git"} if keep_git else SKIP_DIRS
    return any(p in skip for p in parts[:-1]) or parts[-1] in skip or bool(JUNK_FILE_RE.match(parts[-1]))

# Filenames that conventionally hold secrets. Every copy path (untracked files,
# trees, memory, skills) leaves a match behind unless the user picks it by id.
SECRET_FILE_RE = re.compile(
    r"^(\.env(\..+)?|\.envrc|.*\.(pem|key|p12|pfx|jks|keystore|kdbx|tfstate|tfstate\.backup|tfvars)"
    r"|credentials(\.json|\.toml|\.ya?ml)?|.*secrets?.*\.(json|ya?ml|toml|env)|auth\.json|token(s)?\.json"
    r"|\.netrc|_netrc|\.npmrc|\.pypirc|\.git-credentials|\.dockercfg|kubeconfig.*|\.kube|"
    r"id_(rsa|dsa|ecdsa|ed25519)(_sk)?|.*service[-_]?account.*\.json|application_default_credentials\.json)$",
    re.I,
)


def is_secret_file(name: str) -> bool:
    return bool(SECRET_FILE_RE.match(name)) and not name.endswith(".pub")


# Directories whose contents are credentials whatever the filenames are.
SECRET_DIRS = {".ssh", ".gnupg", ".aws", ".azure", ".kube", "gcloud", "gh", ".docker", ".password-store"}
SECRET_FILE_NAMES_ANYWHERE = {".credentials.json", "hosts.yml"}


def is_secret_path(rel: str) -> bool:
    """Whether a path (relative to whatever is being copied) holds a secret:
    by its own name, or by sitting in a credentials directory (.kube/config,
    gh/hosts.yml, .docker/config.json, .ssh/anything)."""
    parts = Path(rel).parts
    if not parts:
        return False
    name = parts[-1]
    if is_secret_file(name) or name in SECRET_FILE_NAMES_ANYWHERE:
        return not name.endswith(".pub")
    return any(p in SECRET_DIRS for p in parts[:-1]) or name in SECRET_DIRS


def file_credential_id(path: Path) -> str:
    """A credential id that names exactly one file: two repos with the same
    name can both hold a .env, and picking one must not pick the other."""
    return f"file:{path}"


# Values that look like live credentials, to scan transcripts and patches for.
# Only the KIND is ever reported, never the match.
SECRET_VALUE_PATTERNS = [
    (re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b"), "AWS access key id"),
    (re.compile(r"aws_secret_access_key\s*[=:]\s*\S{20,}", re.I), "AWS secret key"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "private key"),
    (re.compile(r"\bgh[pousr]_[0-9A-Za-z]{30,}"), "GitHub token"),
    (re.compile(r"\bgithub_pat_[0-9A-Za-z_]{40,}"), "GitHub token"),
    (re.compile(r"\bglpat-[0-9A-Za-z_-]{20,}"), "GitLab token"),
    (re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{10,}"), "Slack token"),
    (re.compile(r"\bsk-(ant-|proj-)?[A-Za-z0-9_-]{32,}"), "API key"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), "Google API key"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}"), "JWT"),
]


# One alternation instead of a loop over patterns: transcripts reach hundreds
# of MB, and a single pass is several times faster.
_SECRET_VALUE_RE = re.compile("|".join(
    f"(?P<p{i}>{'(?i:' + rx.pattern + ')' if rx.flags & re.I else rx.pattern})"
    for i, (rx, _) in enumerate(SECRET_VALUE_PATTERNS)
))


def scan_secret_values(lines: Iterable[str]) -> dict[str, int]:
    """Kinds of credential-looking values in some text, with counts."""
    found: dict[str, int] = {}
    for line in lines:
        for m in _SECRET_VALUE_RE.finditer(line):
            kind = SECRET_VALUE_PATTERNS[int(m.lastgroup[1:])][1]
            found[kind] = found.get(kind, 0) + 1
    return found


_TEXT_USERINFO_RE = re.compile(r"\b([a-z][a-z0-9+.-]*://)[^/@\s]+@", re.I)


def redact_values(text: str) -> str:
    """Text with anything credential-looking replaced by its kind, and URL
    user:password@ parts removed. Redact BEFORE shortening: a cut token is
    too short to match."""
    for rx, kind in SECRET_VALUE_PATTERNS:
        text = rx.sub(f"<redacted {kind}>", text)
    return _TEXT_USERINFO_RE.sub(r"\1<redacted>@", text)


_USERINFO_RE = re.compile(r"^([a-z][a-z0-9+.-]*://)([^/@\s]+)@", re.I)


def redact_url(url: Optional[str]) -> Optional[str]:
    """A remote URL without credentials in it. For http(s) the whole userinfo
    goes (a token is often the "user"); for ssh only a password goes, since
    the user name (git@) is how the server is reached."""
    if not url:
        return url
    m = _USERINFO_RE.match(url)
    if not m:
        return url
    scheme, userinfo = m.group(1), m.group(2)
    if scheme.lower().startswith(("ssh", "git+ssh")):
        return url.replace(m.group(0), f"{scheme}{userinfo.split(':', 1)[0]}@", 1)
    return url.replace(m.group(0), scheme, 1)


_SCP_LIKE_RE = re.compile(r"^(?:[^@/\s]+@)?([^:/\s]+):(?!//)")


def ssh_host(url: Optional[str]) -> Optional[str]:
    """The host of an SSH remote (``git@host:org/repo``, ``ssh://git@host/...``),
    or None for any other kind of URL."""
    if not url:
        return None
    m = re.match(r"^(?:ssh|git\+ssh)://(?:[^@/]+@)?([^:/]+)", url, re.I)
    if m:
        return m.group(1).lower()
    if "://" in url:
        return None
    m = _SCP_LIKE_RE.match(url)
    return m.group(1).lower() if m else None


def _git(repo: Path, *args: str, check: bool = False) -> str:
    # surrogateescape: repo paths and filenames need not be UTF-8.
    proc = subprocess.run(["git", "-C", str(repo), *args], text=True, errors="surrogateescape", capture_output=True)
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed in {repo}: {proc.stderr.strip()}")
    return proc.stdout.strip() if proc.returncode == 0 else ""


def git_toplevel(path: Path) -> Optional[Path]:
    p = path if path.is_dir() else path.parent
    while not p.exists() and p != p.parent:
        p = p.parent
    top = _git(p, "rev-parse", "--show-toplevel") if p.exists() else ""
    return Path(top) if top else None


# --------------------------------------------------------------------------- #
# Repos
# --------------------------------------------------------------------------- #


@dataclass
class Repo:
    path: str
    git: bool
    branch: Optional[str] = None
    head: Optional[str] = None
    remote: Optional[str] = None
    branches: list[str] = field(default_factory=list)
    unpushed_commits: int = 0
    modified_files: int = 0
    untracked_files: int = 0
    untracked_bytes: int = 0
    patch_bytes: int = 0
    history_bytes: int = 0  # object store on disk: an upper bound for the bundle
    tree_bytes: int = 0  # working tree, without SKIP_DIRS
    secret_files: list[str] = field(default_factory=list)  # untracked (or, for a tree, any) secret-looking files
    tracked_secret_files: list[str] = field(default_factory=list)  # already committed: they travel in history
    submodules: int = 0
    worktree: bool = False  # a linked worktree (its .git is a file pointing elsewhere)
    common_dir: Optional[str] = None  # worktrees of one repo share this
    # How it travels: "reference" (re-cloned from its remote on first boot,
    # with only what the remote lacks shipped), "bundle" (whole history
    # shipped), or "tree" (copied as files).
    plan: str = "tree"
    clone_url: Optional[str] = None
    remote_name: Optional[str] = None
    remote_has: list = field(default_factory=list)  # commits the remote has now (git ls-remote), present here
    upstream: Optional[str] = None
    unpushed_bytes: int = 0  # objects in the shipped refs the remote lacks
    # Of unpushed_commits, how many are on the checked-out branch (HEAD), and
    # the other local branches with commits the remote lacks: those travel
    # only with --all-branches, except branches checked out in a worktree.
    head_unpushed: int = 0
    other_branches_unpushed: dict = field(default_factory=dict)  # branch -> commits not on the remote
    all_branches: bool = False
    uses_lfs: bool = False
    plan_note: str = ""
    flagged_in_patch: dict = field(default_factory=dict)
    sessions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _dir_bytes(root: Path, skip: set[str] = SKIP_DIRS, limit_files: Optional[int] = 200_000) -> int:
    total = n = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip]
        for f in filenames:
            n += 1
            if limit_files is not None and n > limit_files:
                return total
            try:
                total += os.lstat(os.path.join(dirpath, f)).st_size
            except OSError:
                pass
    return total


def _z(out: bytes) -> list[str]:
    # fsdecode keeps non-UTF-8 filenames round-trippable (surrogateescape).
    return [os.fsdecode(f) for f in out.split(b"\0") if f]


def untracked(repo: Path) -> list[str]:
    return _z(subprocess.run(
        ["git", "-C", str(repo), "ls-files", "--others", "--exclude-standard", "-z"], capture_output=True,
    ).stdout)


def tree_secret_files(root: Path) -> list[str]:
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for f in filenames:
            rel = os.path.relpath(os.path.join(dirpath, f), root)
            if is_secret_path(rel):
                out.append(rel)
    return sorted(out)


def describe_repo(path: Path, all_branches: bool = False) -> Repo:
    top = git_toplevel(path)
    if top is None:
        r = Repo(path=str(path), git=False, tree_bytes=_dir_bytes(path))
        r.secret_files = tree_secret_files(path)
        return r
    r = Repo(path=str(top), git=True)
    r.worktree = (top / ".git").is_file()
    r.head = _git(top, "rev-parse", "--verify", "--quiet", "HEAD") or None
    r.branch = (_git(top, "rev-parse", "--abbrev-ref", "HEAD") or None) if r.head else None
    r.remote = redact_url(_git(top, "config", "--get", "remote.origin.url") or None)
    r.branches = [b for b in _git(top, "for-each-ref", "--format=%(refname:short)", "refs/heads").splitlines() if b]
    ahead = _git(top, "rev-list", "--count", "@{upstream}..HEAD")
    r.unpushed_commits = int(ahead) if ahead.isdigit() else 0
    status = _git(top, "status", "--porcelain")
    r.modified_files = sum(1 for line in status.splitlines() if line and not line.startswith("??"))
    if not r.head:
        r.secret_files = tree_secret_files(top)
        r.tree_bytes = _dir_bytes(top)
        return r
    for f in untracked(top):
        if is_rebuilt_or_junk(f, keep_git=True):
            continue  # not copied: see stage_git_*
        if is_secret_path(f):
            r.secret_files.append(f)
        r.untracked_files += 1
        try:
            r.untracked_bytes += (top / f).lstat().st_size
        except OSError:
            pass
    tracked = _z(subprocess.run(["git", "-C", str(top), "ls-files", "-z"], capture_output=True).stdout)
    r.tracked_secret_files = [f for f in tracked if is_secret_path(f)]
    r.submodules = sum(1 for line in _git(top, "submodule", "status").splitlines() if line.strip())
    # The staged and the unstaged patch both travel, and a secret staged then
    # reverted in the working tree shows in neither `diff HEAD` nor the files.
    staged = subprocess.run(["git", "-C", str(top), "diff", "--cached", "--binary"], capture_output=True).stdout
    unstaged = subprocess.run(["git", "-C", str(top), "diff", "--binary"], capture_output=True).stdout
    r.patch_bytes = len(staged) + len(unstaged)
    r.flagged_in_patch = scan_secret_values((staged + b"\n" + unstaged).decode(errors="replace").splitlines())
    common = _git(top, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if common:
        r.common_dir = common
        r.history_bytes = _dir_bytes(Path(common) / "objects", skip=set())
    r.tree_bytes = _dir_bytes(top)
    attrs = top / ".gitattributes"
    r.uses_lfs = attrs.is_file() and "filter=lfs" in attrs.read_text(errors="replace")
    plan = reference_plan(top)
    r.upstream, r.remote_name, r.clone_url, r.remote_has, r.plan_note = (
        plan["upstream"], plan["remote"], plan["url"], plan["has"], plan["note"])
    r.plan = "reference" if r.clone_url and r.remote_has else "bundle"
    r.all_branches = all_branches
    if r.plan == "reference":
        refs = shipped_refs(top, all_branches)
        n = _git(top, "rev-list", "--count", *refs, "--not", *r.remote_has)
        r.unpushed_commits = int(n) if n.isdigit() else r.unpushed_commits
        n = _git(top, "rev-list", "--count", "HEAD", "--not", *r.remote_has)
        r.head_unpushed = int(n) if n.isdigit() else 0
        r.other_branches_unpushed = branches_ahead(top, r.remote_has, exclude=refs)
        r.unpushed_bytes = unpushed_bytes(top, r.remote_has, refs)
    return r


def worktree_branches(repo: Path) -> list[str]:
    """Branches checked out in any worktree of ``repo``, including its own."""
    out = []
    for line in _git(repo, "worktree", "list", "--porcelain").splitlines():
        if line.startswith("branch refs/heads/"):
            out.append(line[len("branch "):])
    return out


def shipped_refs(repo: Path, all_branches: bool = False) -> list[str]:
    """The refs whose unpushed commits travel with a repo that is re-cloned:
    by default HEAD and the branches checked out in its worktrees; with
    ``all_branches``, every local branch and tag too."""
    if all_branches:
        return ["--branches", "--tags", "HEAD"]
    return ["HEAD"] + [b for b in worktree_branches(repo) if b]


def branches_ahead(repo: Path, remote_has: list, exclude: list[str] = ()) -> dict:
    """Local branches with commits the remote lacks, and how many, leaving
    out the refs in ``exclude`` (and every branch when that is all of them)."""
    if "--branches" in exclude:
        return {}
    out = {}
    for line in _git(repo, "for-each-ref", "--format=%(refname)", "refs/heads").splitlines():
        if not line or line in exclude:
            continue
        n = _git(repo, "rev-list", "--count", line, "--not", *exclude, *remote_has)
        if n.isdigit() and int(n):
            out[line[len("refs/heads/"):]] = int(n)
    return out


def unpushed_bytes(repo: Path, remote_has: list, refs: Optional[list] = None) -> int:
    """On-disk size of the objects a bundle of the unpushed work would carry."""
    refs = refs or ["--branches", "--tags", "HEAD"]
    objs = subprocess.run(["git", "-C", str(repo), "rev-list", "--objects", *refs,
                           "--not", *remote_has], capture_output=True).stdout
    if not objs.strip():
        return 0
    sizes = subprocess.run(["git", "-C", str(repo), "cat-file", "--batch-check=%(objectsize:disk)"],
                           input=b"\n".join(line.split(b" ", 1)[0] for line in objs.splitlines()),
                           capture_output=True).stdout
    return sum(int(x) for x in sizes.split() if x.isdigit())


def reference_plan(repo: Path) -> dict:
    """Whether a repo can be re-cloned on the agent instead of shipped, and
    what the remote already has.

    Asks the remote itself (``git ls-remote``) rather than trusting the
    remote-tracking refs, which can be stale: a branch force-pushed or deleted
    since the last fetch would leave a bundle whose prerequisites the fresh
    clone lacks. ``has`` lists the remote's current tips that exist here; a
    bundle of everything not reachable from them is self-consistent with a
    clone made now. Empty when the remote cannot be reached or shares nothing
    with this repo, in which case the repo is shipped whole.
    """
    plan = {"upstream": None, "remote": None, "url": None, "has": [], "note": ""}
    upstream = _git(repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}") or None
    remotes = _git(repo, "remote").split()
    remote = upstream.split("/", 1)[0] if upstream and upstream.split("/", 1)[0] in remotes else (
        "origin" if "origin" in remotes else (remotes[0] if remotes else None))
    plan["upstream"], plan["remote"] = upstream, remote
    if not remote:
        plan["note"] = "no remote"
        return plan
    # The configured URL, not `remote get-url`, which applies this machine's
    # insteadOf rewrites: the agent should get the URL as the user wrote it.
    url = redact_url(_git(repo, "config", "--get", f"remote.{remote}.url"))
    if not url or url.startswith(("/", ".", "file:")):
        plan["note"] = "its remote is a path on this machine"
        return plan
    plan["url"] = url
    proc = subprocess.run(["git", "-C", str(repo), "ls-remote", "--heads", "--tags", remote],
                          capture_output=True, text=True, errors="replace", timeout=60,
                          env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_SSH_COMMAND": os.environ.get(
                              "GIT_SSH_COMMAND", "ssh -o BatchMode=yes")})
    if proc.returncode != 0:
        plan["note"] = "its remote could not be reached from here"
        return plan
    tips = {line.split()[0] for line in proc.stdout.splitlines() if line.strip() and not line.endswith("^{}")}
    plan["has"] = sorted(t for t in tips if _has_commit(repo, t))
    if not plan["has"]:
        plan["note"] = "it shares no commits with its remote"
    return plan


def _has_commit(repo: Path, sha: str) -> bool:
    return subprocess.run(["git", "-C", str(repo), "cat-file", "-e", f"{sha}^{{commit}}"],
                          capture_output=True).returncode == 0


def repos_for(cwds: Iterable[str], paths: Iterable[str], home: Path) -> list[Path]:
    """The project directories behind the cwds and tool paths a session
    recorded: git toplevels, plus any working directory that is not in a git
    repo (it travels as a plain tree).

    Paths that no longer exist are skipped, as are the home directory itself
    (a dotfiles repo at ~ is not the project) and the filesystem root.
    """
    tops: list[Path] = []
    cwds = [c for c in cwds if c]
    for raw in cwds + [p for p in paths if p]:
        p = Path(os.path.expanduser(raw))
        if not p.is_absolute() or not p.exists():
            continue
        top = git_toplevel(p)
        if top is None and raw in cwds and p.is_dir():
            top = p.resolve()  # a working directory outside any repo
        if top is None or top in (home, Path("/")) or top in tops:
            continue
        if any(top != t and t in top.parents for t in tops if not is_git(t)):
            continue  # inside a tree already being copied whole
        tops.append(top)
    return tops


def is_git(path: Path) -> bool:
    return git_toplevel(path) == path


def has_commits(repo: Path) -> bool:
    return bool(_git(repo, "rev-parse", "--verify", "--quiet", "HEAD"))


# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #

# Shell builtins and the base system every Linux image already has; counting
# them would bury the tools that matter.
BASE_COMMANDS = set("""
alias awk basename bash break cat cd chmod hash whereis chown clear command comm cp curl cut date dd declare df diff dirname du echo
egrep env eval exec exit export expr false fgrep file find for fi grep gunzip gzip head history hostname id if in jobs
kill less let ln local ls mkdir mktemp more mv nohup popd printf ps pushd pwd read readlink realpath return rm rmdir sed
seq set sh shift sleep sort source stat sudo tail tar tee test then time timeout touch tr trap true type ulimit umask
unalias uname uniq unset unzip until wait watch wc wget which while whoami xargs yes zip zsh done do else elif case esac
function select tput sha256sum md5sum shasum base64 nproc free top kill pkill pgrep lsof open pbcopy pbpaste
""".split())

# Binary -> install hints on Linux, for the common tools whose package name or
# installer differs from the binary name. Anything else falls back to how it was
# installed locally (npm/pip/cargo/uv) or to a TODO line.
KNOWN_TOOLS: dict[str, dict[str, str]] = {
    "gh": {"apt": "gh", "brew": "gh"},
    "jq": {"apt": "jq", "brew": "jq"},
    "yq": {"brew": "yq", "pip": "yq"},
    "rg": {"apt": "ripgrep", "brew": "ripgrep"},
    "fd": {"apt": "fd-find", "brew": "fd"},
    "tree": {"apt": "tree", "brew": "tree"},
    "make": {"apt": "make"},
    "git": {"apt": "git"},
    "python": {"apt": "python3"},
    "python3": {"apt": "python3"},
    "pip": {"apt": "python3-pip"},
    "pip3": {"apt": "python3-pip"},
    "node": {"apt": "nodejs", "brew": "node"},
    "npm": {"apt": "npm", "brew": "node"},
    "npx": {"apt": "npm", "brew": "node"},
    "pnpm": {"npm": "pnpm"},
    "yarn": {"npm": "yarn"},
    "go": {"apt": "golang-go", "brew": "go"},
    "cargo": {"script": "curl -fsSL https://sh.rustup.rs | sh -s -- -y"},
    "rustc": {"script": "curl -fsSL https://sh.rustup.rs | sh -s -- -y"},
    "uv": {"script": "curl -LsSf https://astral.sh/uv/install.sh | sh"},
    "uvx": {"script": "curl -LsSf https://astral.sh/uv/install.sh | sh"},
    "kubectl": {"brew": "kubectl"},
    "helm": {"brew": "helm"},
    "kind": {"brew": "kind"},
    "k9s": {"brew": "k9s"},
    "terraform": {"brew": "terraform"},
    "aws": {"pip": "awscli"},
    "gcloud": {"todo": "install the Google Cloud CLI: https://cloud.google.com/sdk/docs/install"},
    "az": {"pip": "azure-cli"},
    "docker": {"todo": "docker needs a daemon; Union agents usually build images with flyte instead"},
    "flyte": {"uv": "flyte"},
    "pytest": {"pip": "pytest"},
    "ruff": {"uv": "ruff"},
    "black": {"uv": "black"},
    "mypy": {"pip": "mypy"},
    "tsc": {"npm": "typescript"},
    "eslint": {"npm": "eslint"},
    "prettier": {"npm": "prettier"},
    "claude": {"npm": "@anthropic-ai/claude-code"},
    "codex": {"npm": "@openai/codex"},
    "psql": {"apt": "postgresql-client", "brew": "libpq"},
    "redis-cli": {"apt": "redis-tools", "brew": "redis"},
    "sqlite3": {"apt": "sqlite3", "brew": "sqlite"},
    "ffmpeg": {"apt": "ffmpeg", "brew": "ffmpeg"},
    "protoc": {"apt": "protobuf-compiler", "brew": "protobuf"},
    "buf": {"brew": "bufbuild/buf/buf"},
    "just": {"brew": "just", "cargo": "just"},
}

_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_HEREDOC_RE = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
_WRAPPERS = {"sudo", "env", "time", "nohup", "exec", "command", "builtin", "nice", "timeout", "xargs",
             "then", "do", "else", "elif", "if", "while", "until", "!", "{", "(", "}", ")"}
# Options of a wrapper that take a value, so the value is not the program.
_WRAPPER_VALUE_OPTS = {"-u", "-C", "-n", "-s", "-k", "--signal", "--kill-after"}


def split_segments(command: str) -> list[str]:
    """Split a shell command line into simple commands at ``&& || ; | &``,
    newlines and ``$(`` / backticks, outside quotes. Heredoc bodies are
    dropped: they are data, not commands."""
    segments, cur = [], []
    i, n = 0, len(command)
    quote = None
    pending_heredocs: list[str] = []
    while i < n:
        c = command[i]
        if quote:
            cur.append(c)
            if c == "\\" and quote == '"' and i + 1 < n:
                cur.append(command[i + 1])
                i += 2
                continue
            if c == quote:
                quote = None
            i += 1
            continue
        if c in "'\"":
            quote = c
            cur.append(c)
            i += 1
            continue
        if c == "\\" and i + 1 < n:
            if command[i + 1] != "\n":
                cur.append(command[i:i + 2])
            i += 2
            continue
        if c == "<" and command.startswith("<<", i) and not command.startswith("<<<", i):
            m = _HEREDOC_RE.match(command, i)
            if m:
                pending_heredocs.append(m.group(2))
                cur.append(m.group(0))
                i = m.end()
                continue
        if c == "\n" or c in ";|&" or (c == "$" and command.startswith("$(", i)) or c == "`":
            segments.append("".join(cur))
            cur = []
            if c == "\n" and pending_heredocs:
                # Skip each heredoc body up to its terminator line.
                for tag in pending_heredocs:
                    j = i + 1
                    while j <= n:
                        end = command.find("\n", j)
                        line = command[j:end if end != -1 else n]
                        j = (end + 1) if end != -1 else n + 1
                        if line.strip() == tag:
                            break
                    i = j - 1
                pending_heredocs = []
            i += 2 if command.startswith(("&&", "||", "$("), i) else 1
            continue
        if c == "#" and (not cur or cur[-1].isspace()):
            end = command.find("\n", i)
            i = n if end == -1 else end
            continue
        cur.append(c)
        i += 1
    segments.append("".join(cur))
    return [seg.strip() for seg in segments if seg.strip()]


# Asking whether a program exists, or which version it is, is not using it.
_PROBE_ARGS = ({"--version"}, {"-V"}, {"version"}, {"--help"}, {"-h"}, {"help"})


def command_invocations(command: str) -> list[tuple[str, list[str]]]:
    """(program, arguments) for each simple command in a shell command line.
    ``command -v X`` and ``command -V X`` are probes, not runs of X, and are
    left out (as are ``which``/``type``/``hash X``, which run only themselves)."""
    out = []
    for segment in split_segments(command):
        try:
            words = shlex.split(segment)
        except ValueError:
            words = segment.split()
        i = 0
        while i < len(words):
            w = words[i]
            prev = words[i - 1] if i else ""
            if w == "command" and i + 1 < len(words) and words[i + 1] in ("-v", "-V"):
                break  # a probe: `command -v X` asks whether X exists
            if _ASSIGN_RE.match(w) or w in _WRAPPERS:
                i += 1
                continue
            if w.startswith("-"):
                i += 2 if w in _WRAPPER_VALUE_OPTS else 1
                continue
            if prev == "timeout" and re.match(r"^\d", w):
                i += 1
                continue
            name = os.path.basename(w)
            if re.match(r"^[A-Za-z0-9][\w.+-]*$", name):
                out.append((name, words[i + 1:]))
            break
    return out


def command_names(command: str) -> list[str]:
    """The program each simple command in a shell command line runs."""
    return [name for name, _ in command_invocations(command)]


# How shells report a program that is not there: bash ("x: command not
# found"), zsh ("command not found: x") and dash ("x: not found").
_NOT_FOUND_RE = re.compile(r"command not found: ([\w.+-]+)|([\w.+-]+): command not found(?!:)|\b\d+: ([\w.+-]+): not found")


def not_found_names(text: str) -> set[str]:
    """Programs a shell said it could not find, in some tool output."""
    return {next(g for g in m.groups() if g) for m in _NOT_FOUND_RE.finditer(text or "")}


def command_dirs(command: str, cwd: Optional[str] = None) -> list[str]:
    """Directories a shell command line moves into or points git at
    (``cd DIR``, ``pushd DIR``, ``git -C DIR``): repos a session worked in
    through its shell rather than its file tools. Relative paths resolve
    against ``cwd`` when it is known."""
    dirs = []
    for segment in split_segments(command):
        try:
            words = shlex.split(segment)
        except ValueError:
            continue
        while words and (_ASSIGN_RE.match(words[0]) or words[0] in ("sudo", "env", "time", "nohup", "exec", "command")):
            words = words[1:]
        if not words:
            continue
        target = None
        if words[0] in ("cd", "pushd") and len(words) > 1:
            target = words[1]
        elif os.path.basename(words[0]) == "git" and "-C" in words[1:-1]:
            target = words[words.index("-C", 1) + 1]
        if not target or target.startswith("-"):
            continue
        target = os.path.expanduser(target)
        if not os.path.isabs(target):
            if not cwd:
                continue
            target = os.path.normpath(os.path.join(cwd, target))
        dirs.append(target)
    return dirs


@dataclass
class Tool:
    name: str
    count: int
    local_path: Optional[str] = None
    installed_by: Optional[str] = None  # brew | npm | pip | uv | pipx | cargo | go | system | project | unknown
    package: Optional[str] = None
    linux_install: Optional[str] = None  # one shell line, or None when the image should already have it

    def to_dict(self) -> dict:
        return asdict(self)


def _installed_by(name: str, path: Optional[str], home: Path) -> tuple[str, Optional[str]]:
    if not path:
        return "unknown", None
    real = os.path.realpath(path)
    m = re.search(r"/Cellar/([^/]+)/", real)
    if m or real.startswith(("/opt/homebrew/", "/home/linuxbrew/")):
        return "brew", m.group(1) if m else name
    m = re.search(r"/node_modules/((?:@[^/]+/)?[^/]+)/", real)
    if m:
        return "npm", m.group(1)
    m = re.search(r"/uv/tools/([^/]+)/", real)
    if m:
        return "uv", m.group(1)
    m = re.search(r"/pipx/venvs/([^/]+)/", real)
    if m:
        return "pipx", m.group(1)
    if real.startswith(str(home / ".cargo")):
        return "cargo", name
    if real.startswith(str(home / "go" / "bin")):
        return "go", name
    if real.startswith(str(home)):
        return "project", None
    return "system", None


def linux_install_line(tool: Tool) -> Optional[str]:
    """One shell line that installs ``tool`` on a Linux agent, or None when
    there is nothing to install. Known tools first, then however the tool was
    installed locally, then a TODO the agent (or the user) fills in."""
    hint = KNOWN_TOOLS.get(tool.name, {})
    pkg = tool.package or tool.name
    if "script" in hint:
        return hint["script"]
    if "apt" in hint:
        return f"apt_install {hint['apt']}"
    if "npm" in hint:
        return f"npm install -g {hint['npm']}"
    if "uv" in hint:
        return f"uv tool install {hint['uv']}"
    if "pip" in hint:
        return f"pip install --user {hint['pip']}"
    if tool.installed_by == "npm":
        return f"npm install -g {pkg}"
    if tool.installed_by in ("uv", "pipx"):
        return f"uv tool install {pkg}"
    if tool.installed_by == "cargo":
        return f"cargo install {hint.get('cargo', pkg)}"
    if tool.installed_by == "go":
        return f"# TODO go install <module>@latest   # {tool.name}: module path unknown"
    if "brew" in hint or tool.installed_by == "brew":
        return f"brew_install {hint.get('brew', pkg)}"
    if "todo" in hint:
        return f"# TODO {hint['todo']}"
    if tool.installed_by in ("system", "project"):
        return None  # part of the OS, or lives in a repo/venv that travels with it
    return f"# TODO {tool.name}: not found locally, so how to install it is unknown"


def tools_from(commands: Iterable[str], home: Path, not_found: Iterable[str] = ()) -> list[Tool]:
    """The programs the commands ran, with how to install each on Linux.

    Left out: the base system, probes (``command -v X``, ``X --version``
    when that is all a session did with X), and programs a shell reported
    not found that are not installed here either: the session only checked
    for them."""
    counts: Counter = Counter()
    real: set[str] = set()
    for c in commands:
        for name, args in command_invocations(c):
            if name in BASE_COMMANDS:
                continue
            counts[name] += 1
            if set(args) not in _PROBE_ARGS:
                real.add(name)
    missing = set(not_found)
    out = []
    for name, count in counts.most_common():
        path = shutil.which(name)
        if name not in real:
            continue  # only ever asked for its version or help
        if path is None and name in missing:
            continue  # the shell said it was not there, and it is not here either
        if path is None and name not in KNOWN_TOOLS and not re.match(r"^[a-z][a-z0-9_+-]*$", name):
            continue  # not a program anyone could install: a stray word, a path, a variable
        by, pkg = _installed_by(name, path, home)
        t = Tool(name, count, path, by, pkg)
        t.linux_install = linux_install_line(t)
        out.append(t)
    return out


# --------------------------------------------------------------------------- #
# Credentials
# --------------------------------------------------------------------------- #


@dataclass
class Credential:
    id: str
    kind: str
    path: str  # absolute local path
    rel: Optional[str]  # path relative to home, where it would land remotely
    advice: str
    copyable: bool = True

    def to_dict(self) -> dict:
        return asdict(self)


# (id, kind, path relative to home, what to do instead of copying it)
CREDENTIAL_PATHS = [
    ("gh", "GitHub CLI login", ".config/gh/hosts.yml", "run `gh auth login` in the agent, or give it a GH_TOKEN secret"),
    ("git-credentials", "git credential store", ".git-credentials", "sign in again in the agent"),
    ("netrc", "netrc logins", ".netrc", "sign in again in the agent"),
    ("aws", "AWS credentials", ".aws/credentials", "use a project secret or an IAM role for the agent"),
    ("aws-config", "AWS config and profiles", ".aws/config", "safe to copy if it holds only profiles and regions"),
    ("aws-sso", "AWS SSO token cache", ".aws/sso/cache", "run `aws sso login` in the agent instead"),
    ("gcloud", "Google Cloud login", ".config/gcloud", "run `gcloud auth login` in the agent, or use a service account secret"),
    ("azure", "Azure CLI login", ".azure", "run `az login` in the agent"),
    ("kubeconfig", "Kubernetes contexts", ".kube/config", "copying gives the agent your cluster access; prefer a scoped kubeconfig secret"),
    ("docker", "Docker registry logins", ".docker/config.json", "use a registry secret instead"),
    ("npmrc", "npm registry token", ".npmrc", "use a project secret for the token"),
    ("pypirc", "PyPI upload token", ".pypirc", "use a project secret for the token"),
    ("cargo", "crates.io token", ".cargo/credentials.toml", "use a project secret for the token"),
    ("terraform", "Terraform Cloud token", ".terraform.d/credentials.tfrc.json", "use a project secret for the token"),
    ("flyte", "Flyte/Union CLI config", ".flyte/config.yaml", "the agent already runs inside Union; usually not needed"),
    ("union", "Union CLI config", ".union/config.yaml", "the agent already runs inside Union; usually not needed"),
]

# Never offered: the agent signs in to its own model provider.
AGENT_LOGINS = [
    ("claude-login", "Claude Code login", ".claude/.credentials.json"),
    ("codex-login", "Codex login", ".codex/auth.json"),
]

FLAGGED_ENV_NAME_RE = re.compile(r"(TOKEN|SECRET|PASSWORD|PASSWD|API_KEY|APIKEY|ACCESS_KEY|PRIVATE_KEY|CREDENTIAL)", re.I)


# Where a tool reads its credentials from when an environment variable says
# so, instead of the default path under home: (credential id, variable, file
# inside the directory the variable names, or "" when it names the file).
ENV_LOCATIONS = [
    ("gh", "GH_CONFIG_DIR", "hosts.yml"),
    ("aws", "AWS_SHARED_CREDENTIALS_FILE", ""),
    ("aws-config", "AWS_CONFIG_FILE", ""),
    ("gcloud", "CLOUDSDK_CONFIG", "."),
]


def _gh_holds_token(path: Path) -> bool:
    """Whether a gh hosts.yml holds a token. Recent gh keeps it in the system
    keyring (the macOS Keychain) and writes only the user name here. Only
    the key's presence is read, never its value."""
    try:
        return any(re.match(r"^\s*oauth_token\s*:\s*\S", line) for line in path.read_text(errors="replace").splitlines())
    except OSError:
        return False


def credentials(home: Path, repos: Iterable[Repo] = (), env: Optional[dict] = None) -> tuple[list[Credential], list[str]]:
    """Credential files found, and the NAMES of secret-looking environment
    variables (values are never read into the report).

    A tool pointed elsewhere by its environment variable (AWS_CONFIG_FILE,
    AWS_SHARED_CREDENTIALS_FILE, CLOUDSDK_CONFIG, GH_CONFIG_DIR or
    XDG_CONFIG_HOME, KUBECONFIG) is read from there, since that is the file
    it actually uses; it still lands at the standard path in the agent."""
    env = os.environ if env is None else env
    overrides: dict[str, tuple[Path, str]] = {}
    for cid, var, inner in ENV_LOCATIONS:
        if env.get(var):
            base = Path(env[var]).expanduser()
            overrides[cid] = (base / inner if inner and inner != "." else base, var)
    if "gh" not in overrides and env.get("XDG_CONFIG_HOME"):
        overrides["gh"] = (Path(env["XDG_CONFIG_HOME"]).expanduser() / "gh" / "hosts.yml", "XDG_CONFIG_HOME")
    found: list[Credential] = []
    for cid, kind, rel, advice in CREDENTIAL_PATHS:
        p, var = overrides.get(cid, (home / rel, ""))
        if not p.exists():
            continue
        c = Credential(cid, kind + (f" (from ${var})" if var else ""), str(p), rel, advice)
        if cid == "gh" and not _gh_holds_token(p):
            c.kind = "GitHub CLI login, token kept in the system keyring"
            c.advice = "this copies no token (gh keeps it in the keyring, not the file); run `gh auth login` in the agent"
            c.copyable = False
        found.append(c)
    ssh = home / ".ssh"
    if ssh.is_dir():
        for key in sorted(ssh.glob("id_*")):
            if key.suffix != ".pub" and key.is_file():
                found.append(Credential(f"ssh:{key.name}", "SSH private key", str(key), f".ssh/{key.name}",
                                        "add a deploy key or use HTTPS with a token instead"))
    kubeconfig = env.get("KUBECONFIG")
    if kubeconfig:
        # The first file lands at the standard ~/.kube/config; any others
        # beside it, and the agent's KUBECONFIG must then list them.
        files = [Path(k).expanduser() for k in kubeconfig.split(os.pathsep) if k]
        files = [k for k in files if k.is_file()]
        found = [c for c in found if c.id != "kubeconfig"] if files else found
        for i, kp in enumerate(files):
            if i == 0:
                found.append(Credential("kubeconfig", "Kubernetes contexts (from $KUBECONFIG)", str(kp), ".kube/config",
                                        "copying gives the agent your cluster access; prefer a scoped kubeconfig secret"))
            else:
                # Numbered, so two files with the same name (dev/config,
                # prod/config) never land on one path.
                found.append(Credential(f"kubeconfig:{i}", "Kubernetes contexts (from $KUBECONFIG)", str(kp),
                                        f".kube/config-{i}", "prefer a scoped kubeconfig secret; the agent's KUBECONFIG"
                                        f" must list ~/.kube/config-{i} too"))
    for cid, kind, rel in AGENT_LOGINS:
        p = home / rel
        if p.exists():
            found.append(Credential(cid, kind, str(p), rel, "never copied; the Union agent signs in on its own", copyable=False))
    for repo in repos:
        for f in repo.secret_files:
            p = Path(repo.path) / f
            found.append(Credential(file_credential_id(p), "secret-looking file in a repo (not committed)",
                                    str(p), None, "use a project secret, or copy it if the repo needs it"))
    names = sorted(k for k in env if FLAGGED_ENV_NAME_RE.search(k))
    return found, names
