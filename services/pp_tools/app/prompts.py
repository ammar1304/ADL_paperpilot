"""The prompt library: versioned prompt files, loaded and rendered by name.

Layout (the graded deliverable lives in the repo's prompts/ folder):

    prompts/
      active.json                 {"P1-decomposer": "v1", ...}: version in use
      P1-decomposer/v1.md         one file per version, never edited once used
      failure_log.md              every observed failure, with the version that fixed it

A version file has a front-matter header, free documentation, and exactly two
sections that are sent to the model:

    ---
    name: P1-decomposer
    version: v1
    technique: ...
    ---
    (purpose, changelog, known failures: documentation only)
    ## System
    ...
    ## User
    ... {{idea_text}} ...

Variables use {{name}}. Rendering fails if a variable is missing, so a prompt
never reaches the model with a hole in it. Single braces (JSON examples) are
left alone.
"""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

_VAR = re.compile(r"\{\{\s*(\w+)\s*\}\}")
_SECTION = re.compile(r"^## (System|User)\s*$", re.MULTILINE)


class PromptError(Exception):
    pass


@dataclass(frozen=True)
class Prompt:
    name: str
    version: str
    system: str
    user: str
    meta: dict[str, str] = field(default_factory=dict)

    @property
    def variables(self) -> set[str]:
        return set(_VAR.findall(self.system)) | set(_VAR.findall(self.user))

    def render(self, variables: dict[str, object]) -> "RenderedPrompt":
        missing = self.variables - set(variables)
        if missing:
            raise PromptError(f"{self.name} {self.version}: missing variables {sorted(missing)}")

        def fill(text: str) -> str:
            return _VAR.sub(lambda m: str(variables[m.group(1)]), text)

        return RenderedPrompt(self.name, self.version, fill(self.system), fill(self.user))


@dataclass(frozen=True)
class RenderedPrompt:
    name: str
    version: str
    system: str
    user: str


def parse_prompt_file(text: str, source: str = "<prompt>") -> Prompt:
    if not text.startswith("---\n"):
        raise PromptError(f"{source}: must start with a '---' front-matter block")
    try:
        header, body = text[4:].split("\n---\n", 1)
    except ValueError as exc:
        raise PromptError(f"{source}: front matter is not closed with '---'") from exc
    meta: dict[str, str] = {}
    for line in header.splitlines():
        if line.strip() and ":" in line:
            key, value = line.split(":", 1)
            meta[key.strip()] = value.strip()
    for key in ("name", "version"):
        if not meta.get(key):
            raise PromptError(f"{source}: front matter needs '{key}'")

    parts = _SECTION.split(body)
    # parts = [preamble, "System", text, "User", text]
    if len(parts) != 5 or (parts[1], parts[3]) != ("System", "User"):
        raise PromptError(f"{source}: needs exactly one '## System' and one '## User' section, in that order")
    sections = {"System": parts[2].strip(), "User": parts[4].strip()}
    if not sections["System"] or not sections["User"]:
        raise PromptError(f"{source}: System and User sections must not be empty")
    return Prompt(meta["name"], meta["version"], sections["System"], sections["User"], meta)


class PromptLibrary:
    def __init__(self, root: Path) -> None:
        self.root = root

    def active_versions(self) -> dict[str, str]:
        path = self.root / "active.json"
        if not path.exists():
            raise PromptError(f"{path} not found")
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise PromptError(f"{path} must map prompt names to versions")
        return {str(k): str(v) for k, v in data.items()}

    def load(self, name: str, version: str | None = None) -> Prompt:
        """The given version, or the active one. Read from disk on every call so
        that switching the active version takes effect without a restart."""
        if not re.fullmatch(r"P\d+-[a-z0-9-]+", name):
            raise PromptError(f"invalid prompt name {name!r}")
        version = version or self.active_versions().get(name)
        if not version:
            raise PromptError(f"{name} has no active version in active.json")
        if not re.fullmatch(r"v\d+", version):
            raise PromptError(f"invalid version {version!r}")
        path = self.root / name / f"{version}.md"
        if not path.exists():
            raise PromptError(f"{path} not found")
        prompt = parse_prompt_file(path.read_text(encoding="utf-8"), str(path))
        if prompt.name != name or prompt.version != version:
            raise PromptError(f"{path}: front matter says {prompt.name} {prompt.version}")
        return prompt

    def check_all(self) -> list[str]:
        """Load every active prompt; return a list of problems (empty = all good)."""
        problems = []
        try:
            versions = self.active_versions()
        except PromptError as exc:
            return [str(exc)]
        for name, version in versions.items():
            try:
                self.load(name, version)
            except PromptError as exc:
                problems.append(str(exc))
        return problems
