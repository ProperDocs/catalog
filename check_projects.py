import concurrent.futures
import configparser
import subprocess
import sys
import tempfile
import textwrap
from collections.abc import Collection, Mapping, Sequence
from concurrent.futures import Future
from pathlib import Path
from typing import Any, Literal, Required, TypeAlias, TypedDict

import yaml

EntrypointType: TypeAlias = Literal["mkdocs_theme", "mkdocs_plugin", "markdown_extension"]


class Project(TypedDict, total=False):
    name: Required[str]
    category: Required[str]
    labels: Collection[str]
    mkdocs_theme: str | Collection[str]
    mkdocs_plugin: str | Collection[str]
    markdown_extension: str | Collection[str]
    github_id: str
    pypi_id: str


def _get_as_list(mapping: Project, key: EntrypointType) -> Collection[str]:
    names: str | Collection[str] = mapping.get(key, ())
    if isinstance(names, str):
        names = (names,)
    return names


_kind_to_label: Mapping[EntrypointType, str] = {
    "mkdocs_plugin": "plugin",
    "mkdocs_theme": "theme",
    "markdown_extension": "markdown",
}

config: Mapping[str, Any] = yaml.safe_load(Path("projects.yaml").read_text(encoding="utf-8"))

projects: Sequence[Project] = config["projects"]
all_labels: Collection[str] = dict.fromkeys(label["label"] for label in config["labels"])
all_categories: Collection[str] = dict.fromkeys(category["category"] for category in config["categories"])


def check_install_project(project: Project, install_name: str, errors: list[str] | None = None) -> list[str] | None:
    if errors is None:
        errors = []

    with tempfile.TemporaryDirectory(prefix="best-of-mkdocs-") as directory:
        try:
            subprocess.run(
                ["pip", "install", "-U", "--ignore-requires-python", "--no-deps", "--target", directory, install_name],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                check=True,
                timeout=30,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            errors.append(f"Failed {e.cmd}:\n{e.stderr}")
            return None

        entry_points_parser = configparser.ConfigParser()
        try:
            [entry_points_file] = Path(directory).glob("*.dist-info/entry_points.txt")
            entry_points_parser.read_string(entry_points_file.read_text())
        except ValueError:
            pass
        entry_points: dict[str, list[str]] = {
            sect: list(entry_points_parser[sect]) for sect in entry_points_parser.sections()
        }

        for item in _get_as_list(project, "mkdocs_plugin"):
            if item not in entry_points.get("mkdocs.plugins", ()):
                errors.append(f"Missing entry point [mkdocs.plugins] '{item}'.\nInstead got {entry_points}")

        for item in _get_as_list(project, "mkdocs_theme"):
            if item not in entry_points.get("mkdocs.themes", ()):
                errors.append(f"Missing entry point [mkdocs.themes] '{item}'.\nInstead got {entry_points}")

        for item in _get_as_list(project, "markdown_extension"):
            if item not in entry_points.get("markdown.extensions", ()):
                base_path = item.replace(".", "/")
                for pattern in base_path + ".py", base_path + "/__init__.py":
                    path = Path(directory, pattern)
                    if path.is_file() and "makeExtension" in path.read_text(encoding="utf-8"):
                        break
                else:
                    errors.append(
                        f"Missing entry point [markdown.extensions] '{item}'.\n"
                        f"Instead got {entry_points}.\n"
                        f"Also not found as a direct import."
                    )

    return errors


pool = concurrent.futures.ThreadPoolExecutor(4)

# Tracks shadowing: projects earlier in the list take precedence.
available: dict[EntrypointType, dict[str, str]] = {k: {} for k in _kind_to_label}

futures: list[tuple[str, Future[list[str] | None]]] = []

for project in projects:
    errors: list[str] = []

    name = project.get("name")
    if not name:
        errors.append("Project must have a 'name:'")
        continue
    category = project.get("category")
    if not category:
        errors.append("Project must have a 'category:'")
    elif category not in all_categories:
        errors.append(f"Unknown category: {category!r} - should be one of: {', '.join(all_categories)}")
    labels = project.get("labels", ())
    for label in labels:
        if label not in all_labels:
            errors.append(f"Unknown label: {label!r} - should be one of: {', '.join(all_labels)}")

    for kind, label in _kind_to_label.items():
        items = _get_as_list(project, kind)

        if label == "plugin" and "theme" in labels and "plugin" not in labels:
            pass
        elif (label in labels) != bool(items):
            errors.append(f"'{label}' label should be present if and only if '{kind}:' is present")

        for item in items:
            already_available = available[kind].get(item) or (
                kind == "mkdocs_plugin" and available[kind].get(item.split("/")[-1])
            )
            if already_available:
                if kind not in project.get("shadowed", ()):
                    errors.append(
                        f"{kind} '{item.split('/')[-1]}' is present in both project '{already_available}' and '{name}'.\n"
                        f"If that is expected, the later of the two projects will be ignored, "
                        f"and to indicate this, it should contain 'shadowed: [{kind}]'"
                    )
            else:
                available[kind][item] = name

    install_name: str | None = None
    if any(key in project for key in _kind_to_label):
        if "pypi_id" in project:
            install_name = project["pypi_id"]
            if "_" in install_name:
                install_name = install_name.replace("_", "-")
                errors.append(f"'pypi_id' should be '{install_name}' not '{project['pypi_id']}'")
        elif "github_id" in project:
            install_name = f"git+https://github.com/{project['github_id']}"
        else:
            errors.append("Missing 'pypi_id:'")

    fut: Future[list[str] | None]
    if install_name:
        fut = pool.submit(check_install_project, project, install_name, errors)
    else:
        fut = Future()
        fut.set_result(errors)
    futures.append((name, fut))


error_count = 0

for project_name, fut in futures:
    result = fut.result()
    if result:
        error_count += len(result)
        print()
        print(f"{project_name}:")
        for error in result:
            print(textwrap.indent(error.rstrip(), "     "))
            print()
    else:
        print(".", end="")
        sys.stdout.flush()

if error_count:
    print()
    sys.exit(f"Exited with {error_count} errors")
