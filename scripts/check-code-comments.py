from __future__ import annotations

import argparse
import ast
import io
import json
import os
import re
import shlex
import subprocess
import sys
import tokenize
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass
from functools import cache
from pathlib import Path

import tree_sitter_pwsh
import yaml
from pygments.lexers import get_lexer_by_name
from pygments.token import Comment, String
from tree_sitter import Language, Parser
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

BASELINE = "scripts/code-comments-baseline.json"
LANGUAGES = {
    ".py": "python",
    ".pyi": "python",
    ".sh": "bash",
    ".bash": "bash",
    ".ps1": "powershell",
    ".psm1": "powershell",
    ".psd1": "powershell",
    ".vbs": "vbscript",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".html": "html",
    ".htm": "html",
}
SHELLS = {
    "bash": "bash",
    "sh": "bash",
    "pwsh": "powershell",
    "powershell": "powershell",
    "python": "python",
}
PYTHON_DIRECTIVE = re.compile(
    r"#\s*(?:"
    r"(?:ruff|flake8):\s*noqa|"
    r"noqa(?::\s*[A-Z]+\d+(?:\s*,\s*[A-Z]+\d+)*)?|"
    r"type:\s*ignore(?:\[[\w-]+(?:\s*,\s*[\w-]+)*\])?|"
    r"mypy:\s*ignore-errors|"
    r"fmt:\s*(?:off|on|skip)|isort:\s*(?:off|on|skip|skip_file|split)|"
    r"pragma:\s*no\s+(?:cover|branch)"
    r")"
)
SHEBANG = re.compile(r"#![ \t]*/[\w./+-]+(?:[ \t]+[\w./=+-]+)*")
ENCODING = re.compile(r"#\s*(?:-\*-\s*)?coding[:=][ \t]*[-\w.]+(?:\s*-\*-)?")
DOCKER_DIRECTIVE = re.compile(
    r"#\s*(?:syntax\s*=\s*[\w./:@-]+|escape\s*=\s*[\\`]|"
    r"check\s*=\s*(?:skip=(?:all|[A-Za-z,]+)(?:;\s*error=(?:true|false))?|error=(?:true|false)))"
)
HEREDOC = re.compile(r"<<(?P<tabs>-)?[ \t]*(?P<quote>['\"]?)(?P<name>[\w-]+)(?P=quote)")
PYTHON_HEREDOC = re.compile(
    r"""(?:python(?:\d+(?:\.\d+)?)?|["']?\$\{?PYTHON_BIN\}?["']?)\s+-\s*"""
)
DOCKER_HEREDOC = re.compile(
    rf"^\s*(?:ADD|COPY|RUN)(?:\s+--[\w=-]+)*\s+{HEREDOC.pattern}",
    re.IGNORECASE,
)
DOTENV_VALUE = re.compile(
    r"""^[ \t]*(?:export[ \t]+)?(?:'[^'\n]+'|[^=\s#]+)[ \t]*=[ \t]*"""
    r"""(?:'(?:\\'|[^'])*'|"(?:\\"|[^"])*"|[^\n]*?(?=[ \t]+#|$))"""
    r"""[ \t]*(#[^\n]*)?$""",
    re.MULTILINE,
)


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    text: str
    directive: bool


type CommentToken = tuple[int, str, bool]


def language_for(path: str) -> str | None:
    name = Path(path).name.lower()
    if (
        name == "dockerfile"
        or name.startswith("dockerfile.")
        or name.endswith(".dockerfile")
    ):
        return "dockerfile"
    if name == ".env" or name.startswith(".env."):
        return "dotenv"
    if name in {".dockerignore", ".gitattributes", ".gitignore"} or name.endswith(
        ".dockerignore"
    ):
        return "patterns"
    if re.fullmatch(r"(?:requirements|constraints)(?:[-.][^.]+)?\.txt", name):
        return "requirements"
    return LANGUAGES.get(Path(name).suffix)


@cache
def lexer(language: str):
    return get_lexer_by_name(language)


@cache
def powershell_parser() -> Parser:
    return Parser(Language(tree_sitter_pwsh.language()))


def source_comments(language: str, source: str) -> Iterator[CommentToken]:
    if language == "python":
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type == tokenize.COMMENT:
                line, column = token.start
                value = token.string.rstrip()
                yield line, value, directive(language, line, column, value)
        return
    if language == "powershell":
        nodes = [powershell_parser().parse(source.encode("utf-8")).root_node]
        while nodes:
            node = nodes.pop()
            if node.type in {"comment", "requires_statement"}:
                assert node.text is not None
                line, column = node.start_point.row + 1, node.start_point.column
                value = node.text.decode().rstrip()
                yield line, value, directive(language, line, column, value)
            else:
                nodes.extend(reversed(node.children))
        return
    if language in {"patterns", "requirements", "dotenv"}:
        if language == "dotenv":
            pattern = re.compile(
                rf"(?:{DOTENV_VALUE.pattern})|^[ \t]*(#[^\n]*)", re.MULTILINE
            )
            for match in pattern.finditer(source):
                if group := match.lastindex:
                    line = source_position(source, match.start(group))[0]
                    yield line, match[group].rstrip(), False
        else:
            pattern = r"^\s*(#.*)" if language == "patterns" else r"(?:^|\s)(#.*)"
            for line, value in enumerate(source.splitlines(), 1):
                if match := re.search(pattern, value):
                    yield line, match[1].rstrip(), False
        return
    for position, token, value in lexer(language).get_tokens_unprocessed(source + "\n"):
        if token in Comment and token != Comment.Preproc:
            line, column = source_position(source, position)
            value = value.rstrip()
            yield line, value, directive(language, line, column, value)
        elif language == "bash" and token in String and value.startswith("<<"):
            begin, _, body = value.partition("\n")
            if not HEREDOC.fullmatch(begin):
                continue
            command = source[source.rfind("\n", 0, position) + 1 : position].lstrip()
            if PYTHON_HEREDOC.fullmatch(command):
                offset = source_position(source, position)[0]
                python_source = body.rsplit("\n", 1)[0]
                for line, comment, exempt in source_comments("python", python_source):
                    yield offset + line, comment, exempt


def source_position(source: str, offset: int) -> tuple[int, int]:
    return source.count("\n", 0, offset) + 1, offset - source.rfind("\n", 0, offset) - 1


def directive(language: str, line: int, column: int, value: str) -> bool:
    if (
        language in {"python", "bash", "powershell"}
        and line == 1
        and column == 0
        and SHEBANG.fullmatch(value)
    ):
        return True
    if language == "python":
        return bool(
            PYTHON_DIRECTIVE.fullmatch(value)
            or (line <= 2 and ENCODING.fullmatch(value))
        )
    if language == "bash":
        return bool(
            re.fullmatch(
                r"#\s*shellcheck\s+(?:(?:disable|enable)=SC\d+(?:,SC\d+)*|"
                r"shell=(?:sh|bash|dash|ksh|busybox)|source=[\w./-]+)",
                value,
            )
        )
    if language == "powershell":
        return bool(
            re.fullmatch(
                r"#Requires\s+(?:-Version\s+\d+(?:\.\d+)*|-RunAsAdministrator|"
                r"-PSEdition\s+(?:Core|Desktop)|-Modules\s+[\w.,-]+)",
                value,
                flags=re.IGNORECASE,
            )
        )
    return False


def docker_comments(source: str) -> Iterator[CommentToken]:
    directives_open = True
    escape = "\\"
    remaining = enumerate(source.splitlines(), 1)
    pending: list[tuple[int, str]] = []
    for line, value in remaining:
        stripped = value.lstrip()
        if stripped.startswith("#"):
            allowed = directives_open and bool(
                DOCKER_DIRECTIVE.fullmatch(stripped.rstrip())
            )
            if allowed and re.match(r"#\s*escape\s*=", stripped):
                escape = stripped.rstrip()[-1]
            directives_open = allowed
            yield line, stripped.rstrip(), allowed
            continue
        directives_open = False
        if not stripped:
            continue
        pending.append((line, value))
        if value.rstrip().endswith(escape):
            continue
        first = pending[0][1]
        if heredoc := DOCKER_HEREDOC.match(first):
            body = []
            for _, row in remaining:
                delimiter = row.lstrip("\t") if heredoc["tabs"] else row
                if delimiter == heredoc["name"]:
                    break
                body.append(row)
            if first.lstrip().upper().startswith("RUN"):
                interpreter = first[heredoc.end() :].strip()
                language = SHELLS.get(Path(interpreter).name, "bash")
                if interpreter and PYTHON_HEREDOC.fullmatch(interpreter + " -"):
                    language = "python"
                for body_line, comment, exempt in source_comments(
                    language, "\n".join(body)
                ):
                    yield line + body_line, comment, exempt
        else:
            yield from docker_instruction(pending, escape)
        pending.clear()


def docker_instruction(
    rows: list[tuple[int, str]], escape: str
) -> Iterator[CommentToken]:
    _, first = rows[0]
    match = re.match(
        r"[ \t]*(?:RUN|CMD|ENTRYPOINT|HEALTHCHECK\b.*?\bCMD)[ \t]+",
        first,
        re.IGNORECASE,
    )
    if match:
        script = first[match.end() :] + "".join("\n" + value for _, value in rows[1:])
        if script.lstrip().startswith("["):
            try:
                words = json.loads(script.replace(escape + "\n", ""))
            except json.JSONDecodeError:
                words = None
            if isinstance(words, list) and all(isinstance(word, str) for word in words):
                if invoked := invoked_script(words):
                    index, language = invoked
                    for _, value, exempt in source_comments(language, words[index]):
                        yield rows[0][0], value, exempt
                return
        script = script.replace(escape + "\n", "\\\n")
        for line, value, exempt in source_comments("bash", script):
            yield rows[line - 1][0], value, exempt


def mapping(node: Node | None) -> dict[str, Node]:
    if not isinstance(node, MappingNode):
        return {}
    return {
        key.value: value for key, value in node.value if isinstance(key, ScalarNode)
    }


def scalar(node: Node | None) -> str:
    return node.value if isinstance(node, ScalarNode) else ""


def sequence(node: Node | None) -> list[Node]:
    return node.value if isinstance(node, SequenceNode) else []


def shell_default(node: Node | None) -> str:
    defaults = mapping(mapping(node).get("defaults"))
    return scalar(mapping(defaults.get("run")).get("shell"))


def workflow_scripts(document: Node) -> Iterator[tuple[Node, str, str]]:
    workflow = mapping(document)
    jobs = list(mapping(workflow.get("jobs")).values())
    if "runs" in workflow:
        jobs.append(workflow["runs"])
    for job in jobs:
        settings = mapping(job)
        default = shell_default(job) or shell_default(document)
        if not default:
            default = (
                "pwsh"
                if scalar(settings.get("runs-on")).startswith("windows")
                else "bash"
            )
        for step in sequence(settings.get("steps")):
            fields = mapping(step)
            shell = (scalar(fields.get("shell")) or default).split()[0]
            if (run := fields.get("run")) and shell in SHELLS:
                yield run, SHELLS[shell], scalar(run)
            uses = scalar(fields.get("uses"))
            arguments = mapping(fields.get("with"))
            if uses.startswith("appleboy/ssh-action@") and (
                script := arguments.get("script")
            ):
                yield script, "bash", scalar(script)


def compose_words(node: Node | None) -> list[tuple[Node, str]]:
    if isinstance(node, SequenceNode):
        return [(item, scalar(item)) for item in node.value]
    if isinstance(node, ScalarNode):
        return [(node, word) for word in shlex.split(node.value)]
    return []


def invoked_script(words: list[str]) -> tuple[int, str] | None:
    if words and (language := SHELLS.get(Path(words[0]).name)):
        for index, word in enumerate(words[1:-1], 1):
            if re.fullmatch(r"-[a-z]*c[a-z]*", word):
                return index + 1, language
    return None


def compose_scripts(document: Node) -> Iterator[tuple[Node, str, str]]:
    for service in mapping(mapping(document).get("services")).values():
        settings = mapping(service)
        words = compose_words(settings.get("entrypoint")) + compose_words(
            settings.get("command")
        )
        if invoked := invoked_script([word for _, word in words]):
            index, language = invoked
            script, value = words[index]
            yield script, language, value
        healthcheck = mapping(settings.get("healthcheck")).get("test")
        parts = sequence(healthcheck)
        if len(parts) >= 2 and scalar(parts[0]) == "CMD-SHELL":
            yield parts[1], "bash", scalar(parts[1])
        elif isinstance(healthcheck, ScalarNode) and healthcheck.value != "NONE":
            yield healthcheck, "bash", healthcheck.value


def yaml_scripts(path: str, source: str) -> Iterator[tuple[Node, str, str]]:
    if path.startswith((".github/workflows/", ".github/actions/")):
        for document in yaml.compose_all(source, Loader=yaml.SafeLoader):
            yield from workflow_scripts(document)
    elif re.fullmatch(r"(?:docker-)?compose(?:\.[^.]+)*\.ya?ml", Path(path).name):
        for document in yaml.compose_all(source, Loader=yaml.SafeLoader):
            yield from compose_scripts(document)


def embedded_python_module_scripts(source: str) -> Iterator[tuple[int, str, str]]:
    languages = {
        "_CONTAINER_HELPER": "python",
        "_LEGACY_CLEANUP_SHELL": "bash",
        "_LEGACY_RUN_SHELL": "bash",
    }
    for statement in ast.parse(source).body:
        if not isinstance(statement, ast.Assign) or not isinstance(
            statement.value, ast.Constant
        ):
            continue
        for target in statement.targets:
            if (
                isinstance(target, ast.Name)
                and target.id in languages
                and isinstance(statement.value.value, str)
            ):
                yield (
                    statement.value.lineno - 1,
                    languages[target.id],
                    statement.value.value,
                )


def findings(path: str, data: bytes) -> Iterator[Finding]:
    language = language_for(path)
    if language is None:
        return
    encoding = "utf-8-sig"
    if language == "python":
        encoding, _ = tokenize.detect_encoding(io.BytesIO(data).readline)
    elif data.startswith((b"\xff\xfe", b"\xfe\xff")):
        encoding = "utf-16"
    source = data.decode(encoding).replace("\r\n", "\n").replace("\r", "\n")
    comments = (
        docker_comments(source)
        if language == "dockerfile"
        else source_comments(language, source)
    )
    for line, value, exempt in comments:
        yield Finding(path, line, value, exempt)
    if language == "yaml":
        for node, shell, script in yaml_scripts(path, source):
            offset = node.start_mark.line + (
                isinstance(node, ScalarNode) and node.style in {"|", ">"}
            )
            for line, value, exempt in source_comments(shell, script):
                yield Finding(path, offset + line, value, exempt)
    elif path == "scripts/media-acceptance-docker-adapter.py":
        for offset, embedded_language, script in embedded_python_module_scripts(source):
            for line, value, exempt in source_comments(embedded_language, script):
                yield Finding(path, offset + line, value, exempt)


def git(root: Path, *args: str, data: bytes | None = None) -> bytes:
    return subprocess.run(
        ["git", *args], cwd=root, input=data, capture_output=True, check=True
    ).stdout


def git_paths(output: bytes) -> Iterator[str]:
    return (os.fsdecode(path) for path in output.split(b"\0") if path)


def worktree_findings(root: Path) -> Iterator[Finding]:
    paths = git(root, "ls-files", "--cached", "--others", "--exclude-standard", "-z")
    for name in sorted(set(git_paths(paths))):
        if language_for(name) is None:
            continue
        path = root / name
        if path.is_file() and not path.is_symlink():
            yield from file_findings(name, path.read_bytes())


def file_findings(path: str, data: bytes) -> Iterator[Finding]:
    try:
        yield from findings(path, data)
    except (
        IndentationError,
        SyntaxError,
        UnicodeError,
        ValueError,
        tokenize.TokenError,
        yaml.YAMLError,
    ) as error:
        raise ValueError(f"{path}: could not read comments: {error}") from error


def git_blobs(root: Path, revision: str) -> Iterator[tuple[str, bytes]]:
    files = []
    for entry in git(root, "ls-tree", "-r", "-z", revision).split(b"\0"):
        header, _, path = entry.partition(b"\t")
        name = os.fsdecode(path)
        if header.startswith((b"100644 ", b"100755 ")) and language_for(name):
            files.append((name, header.split()[-1]))
    if not files:
        return
    requests = b"\n".join(oid for _, oid in files) + b"\n"
    batch = io.BytesIO(git(root, "cat-file", "--batch", data=requests))
    for path, _ in files:
        _, _, size = batch.readline().split()
        yield path, batch.read(int(size))
        batch.read(1)


def counts(found: Iterable[Finding]) -> Counter[tuple[str, str]]:
    return Counter((item.path, item.text) for item in found if not item.directive)


def read_baseline(raw: bytes) -> Counter[tuple[str, str]]:
    document = json.loads(raw)
    if not isinstance(document, dict):
        raise TypeError("the baseline must be an object keyed by file path")
    result = Counter()
    for path, comments in document.items():
        if not isinstance(comments, dict) or not comments or language_for(path) is None:
            raise ValueError(f"invalid baseline entry for {path}")
        for comment, number in comments.items():
            if not comment.strip() or type(number) is not int or number < 1:
                raise ValueError(f"invalid baseline count for {path}: {comment!r}")
            result[path, comment] = number
    return result


def write_baseline(path: Path, allowed: Counter[tuple[str, str]]) -> None:
    document: dict[str, dict[str, int]] = {}
    for (name, comment), number in sorted(allowed.items()):
        document.setdefault(name, {})[comment] = number
    path.write_text(
        json.dumps(document, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def previous_baseline(root: Path, ref: str) -> Counter[tuple[str, str]]:
    if ref and set(ref) == {"0"}:
        ref = "HEAD^"
    revision = (
        git(root, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}")
        .decode()
        .strip()
    )
    if git(root, "ls-tree", revision, "--", BASELINE):
        return read_baseline(git(root, "show", f"{revision}:{BASELINE}"))
    return counts(
        finding
        for name, data in git_blobs(root, revision)
        for finding in file_findings(name, data)
    )


def report_counts(message: str, differences: Counter[tuple[str, str]]) -> None:
    for (path, comment), number in sorted(differences.items()):
        print(f"{path}: {message} ({number}): {comment!a}", file=sys.stderr)


def check(root: Path, found: list[Finding], base: str, prune: bool) -> int:
    baseline_path = root / BASELINE
    allowed = read_baseline(baseline_path.read_bytes())
    previous = previous_baseline(root, base)
    if growth := allowed - previous:
        report_counts("baseline grew", growth)
        return 1
    actual = counts(found)
    if actual - allowed:
        remaining = allowed.copy()
        for item in found:
            if item.directive:
                continue
            key = item.path, item.text
            if remaining[key] > 0:
                remaining[key] -= 1
            else:
                print(
                    f"{item.path}:{item.line}: new prose comment: {item.text!a}",
                    file=sys.stderr,
                )
        return 1
    if prune:
        write_baseline(baseline_path, actual)
    elif stale := allowed - actual:
        report_counts("unused baseline allowance", stale)
        print("Run python scripts/check-code-comments.py --prune.", file=sys.stderr)
        return 1
    print(f"Code comments check passed ({sum(actual.values())} existing allowances).")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check code comments against a shrinking Git baseline."
    )
    parser.add_argument(
        "--base", default="HEAD", help="trusted Git revision for baseline comparison"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--prune", action="store_true", help="remove unused baseline entries"
    )
    mode.add_argument("--inventory", action="store_true", help="list comments as JSON")
    args = parser.parse_args()
    try:
        root = Path(
            os.fsdecode(git(Path.cwd(), "rev-parse", "--show-toplevel").strip())
        )
        found = sorted(worktree_findings(root), key=lambda item: (item.path, item.line))
        if args.inventory:
            print(json.dumps([asdict(item) for item in found], ensure_ascii=True))
            return 0
        return check(root, found, args.base, args.prune)
    except (OSError, TypeError, ValueError, subprocess.CalledProcessError) as error:
        print(f"Code comments check failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
