"""Canonical package contract for source-owned Hermes packages.

One versioned envelope, four family payloads. This module is the single
source of truth for what a Hermes package manifest *is* — every
authoritative loader, install path, lint surface, and CI census either
imports these primitives or is a documented adapter over them
(see website/docs/developer-guide/package-contract.md).

Families and their manifests:

- ``plugin``    — ``plugins/**/plugin.yaml`` (native plugins; manifest
                  schema v1 default, v2 current — the same
                  ``manifest_version`` semantics ``hermes_cli.plugins``
                  enforces at load time).
- ``plugin`` (dashboard payload) — ``plugins/<name>/dashboard/manifest.json``
                  (web-dashboard extensions; a package may carry BOTH a
                  plugin.yaml and a dashboard manifest — the dashboard is a
                  payload of the same package, never a second package).
- ``skill``     — ``skills/**/SKILL.md`` + ``optional-skills/**/SKILL.md``
                  YAML frontmatter (schema v1, defined here; no new
                  frontmatter field is introduced for versioning).
- ``mcp``       — ``optional-mcps/<name>/manifest.yaml`` (curated MCP
                  catalog; ``manifest_version: 1``).

Design rules (native plugin compatibility contract — see
``website/docs/developer-guide/plugins/index.md#native-plugin-compatibility-contract``):

- Runtime loaders stay warn-and-continue; unknown manifest fields are
  *reported* here but never fatal to a load. Fail-closed enforcement
  belongs to authoring/install/CI boundaries (``hermes packages lint``,
  ``hermes plugins doctor``, skill create/edit, the inventory test).
- Public identity is preserved: a plugin's id is its path-derived
  registry key, a skill's id is its directory name, an MCP entry's id is
  its catalog directory name.
- Per-family schema versions are retained (``manifest_version`` for
  plugin.yaml and mcp manifest.yaml); the contract does not add a
  monolithic cross-family version literal to any manifest.

Everything here is pure: no logging side effects, no I/O beyond the
explicit ``enumerate_source_packages`` walk, deterministic ordering of
records and findings.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from agent.skill_utils import (
    SKILL_PROMPT_DESC_LIMIT,
    parse_frontmatter,
    yaml_load,
)

# ── Contract constants ────────────────────────────────────────────────────

#: Version of this package contract (envelope semantics + family payloads).
CONTRACT_VERSION = 1

#: Highest plugin.yaml manifest schema version understood (mirrors the
#: loader; hermes_cli.plugins re-exports this as SUPPORTED_MANIFEST_VERSION).
PLUGIN_MANIFEST_SUPPORTED = 2

#: SKILL.md frontmatter schema version defined by this contract. Implicit —
#: skills do not declare it in frontmatter.
SKILL_SCHEMA_SUPPORTED = 1

#: optional-mcps manifest.yaml schema version. The contract owns this
#: value; ``hermes_cli.mcp_catalog`` mirrors it as its own literal (that
#: reader is the fail-closed supply-chain install boundary and stays free
#: of extra import chains). Equality is enforced by
#: tests/test_package_contract.py.
MCP_MANIFEST_SUPPORTED = 1

#: Platform tokens accepted in ``platforms:`` lists across families.
#: linux/macos/windows map through agent.skill_utils.PLATFORM_MAP (macos →
#: darwin, so a literal ``darwin`` matches too); termux/android are the
#: Termux-session tags the matcher special-cases.
KNOWN_PLATFORMS = frozenset(
    {"linux", "macos", "windows", "darwin", "termux", "android"}
)

#: Native plugin kinds (single source of truth; hermes_cli.plugins
#: re-exports this as _VALID_PLUGIN_KINDS).
VALID_PLUGIN_KINDS = frozenset(
    {"standalone", "backend", "exclusive", "platform", "model-provider"}
)

#: Category directory → expected plugin kind. Census-driven: this is how
#: each family's own discovery system actually routes its packages.
#: Categories not listed here (observability) carry standalone opt-in
#: plugins, and flat packages declare their own kind.
PLUGIN_CATEGORY_KINDS: Dict[str, str] = {
    "browser": "backend",
    "cron_providers": "exclusive",
    "dashboard_auth": "backend",
    "image_gen": "backend",
    "memory": "exclusive",
    "model-providers": "model-provider",
    "platforms": "platform",
    "video_gen": "backend",
    "web": "backend",
}

#: plugin.yaml fields the current contract understands. Anything else is
#: forward-compat surface: reported by the validator, ignored (never fatal)
#: by loaders. hermes_cli.plugins re-exports this as _KNOWN_MANIFEST_FIELDS.
KNOWN_PLUGIN_MANIFEST_FIELDS: frozenset = frozenset(
    {
        # v1
        "name", "version", "description", "author", "requires_env",
        "provides_tools", "provides_hooks", "kind", "hooks", "label",
        "optional_env", "platforms", "external_dependencies", "pip_dependencies",
        "provides_browser_providers", "provides_web_providers",
        # v2 (#64165)
        "manifest_version", "api_version", "requires_plugins",
        "python_dependencies", "config_schema", "license", "homepage", "tags",
        # owned by sibling sub-issues but reserved so their manifests don't warn
        "capabilities", "emits", "listens", "hermes", "depends",
    }
)

#: SKILL.md top-level frontmatter fields with a consumer or an envelope
#: role. Sources: agent/skill_utils.py (platforms, environments),
#: tools/skills_tool.py (prerequisites, setup,
#: required_environment_variables, required_credential_files,
#: compatibility), the skill authoring standards (name, description,
#: version, author, license, metadata), the loader's top-level tag/category
#: mirroring, and ``dependencies`` (declared pip requirements — the skill
#: counterpart of plugin ``pip_dependencies``; surfaced, never installed).
KNOWN_SKILL_FRONTMATTER_FIELDS = frozenset(
    {
        "name", "description", "version", "author", "license", "platforms",
        "metadata", "tags", "category", "dependencies", "prerequisites",
        "setup", "required_environment_variables", "required_credential_files",
        "environments", "compatibility",
    }
)

#: metadata.hermes.* keys with a consumer or documented meaning.
#: ``upstream`` / ``supersedes`` / ``related_docs`` / ``credits`` are
#: provenance metadata (documented in the package-contract guide; no
#: runtime consumer, surfaced in listings only). ``session_platforms``
#: gates a skill onto named gateway channels (agent/skill_utils.py ->
#: agent/prompt_builder.py).
KNOWN_SKILL_HERMES_KEYS = frozenset(
    {
        "tags", "category", "related_skills", "homepage", "config",
        "requires_toolsets", "fallback_for_toolsets", "requires_tools",
        "fallback_for_tools", "credits", "upstream", "supersedes",
        "related_docs", "session_platforms",
    }
)

#: optional-mcps manifest.yaml fields (mirrors hermes_cli.mcp_catalog
#: _parse_manifest — the install-time fail-closed reader). ``suggest``
#: carries the desktop composer's brand-pill triggers and is parsed there
#: into a ``SuggestSpec``; the shape is validated by that reader, not
#: re-validated here (one validator per boundary).
KNOWN_MCP_MANIFEST_FIELDS = frozenset(
    {
        "manifest_version", "name", "description", "source", "transport",
        "auth", "install", "post_install", "tools", "suggest",
    }
)

#: dashboard/manifest.json fields (mirrors hermes_cli.web_server
#: _discover_dashboard_plugins).
KNOWN_DASHBOARD_MANIFEST_FIELDS = frozenset(
    {
        "name", "label", "description", "icon", "version", "tab", "slots",
        "entry", "css", "api",
    }
)

#: Semver core with optional pre-release/build metadata.
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.\-]+)?$")

ERROR = "error"
WARNING = "warning"


# ── Data model ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Finding:
    """One validation result. Deterministic, orderable, JSON-safe."""

    severity: str  # ERROR | WARNING
    rule: str      # stable kebab-case id, e.g. "invalid-version"
    package: str   # "<family>:<id>" (or a path for pre-identity findings)
    message: str

    def to_dict(self) -> Dict[str, str]:
        return {
            "severity": self.severity,
            "rule": self.rule,
            "package": self.package,
            "message": self.message,
        }


@dataclass
class PackageRecord:
    """Normalized envelope + family payload for one source-owned package."""

    family: str                 # "plugin" | "skill" | "mcp"
    id: str                     # family-scoped public identity (preserved)
    path: str                   # repo-relative package directory
    manifest_path: str          # repo-relative manifest file
    ownership: str              # "bundled" | "optional"
    schema_version: int         # per-family manifest schema version
    name: str = ""
    version: str = ""
    description: str = ""
    author: str = ""
    license: str = ""
    homepage: str = ""
    kind: str = ""              # plugin kind; "dashboard" for dashboard-app
    category: str = ""          # skill category path / plugin category dir
    platforms: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    capabilities: List[str] = field(default_factory=list)
    # {"python": [...], "plugins": [{id, version_range}], "env": [...],
    #  "optional_env": [...], "commands": [...], "external": [...]}
    dependencies: Dict[str, Any] = field(default_factory=dict)
    # family-specific: plugin {"register": bool, "hooks": [...]};
    # skill {"body": True}; mcp {"transport": ..., "command"/"url": ...};
    # dashboard {"entry": ..., "css": ..., "api": ...}
    entrypoints: Dict[str, Any] = field(default_factory=dict)
    # residual normalized family payload (provides_*, tab, auth, ...)
    payload: Dict[str, Any] = field(default_factory=dict)
    unknown_fields: List[str] = field(default_factory=list)

    @property
    def ref(self) -> str:
        return f"{self.family}:{self.id}"


def record_to_dict(rec: PackageRecord) -> Dict[str, Any]:
    """JSON-safe, deterministically ordered dict for one record."""
    return {
        "contract_version": CONTRACT_VERSION,
        "family": rec.family,
        "id": rec.id,
        "path": rec.path,
        "manifest_path": rec.manifest_path,
        "ownership": rec.ownership,
        "schema_version": rec.schema_version,
        "name": rec.name,
        "version": rec.version,
        "description": rec.description,
        "author": rec.author,
        "license": rec.license,
        "homepage": rec.homepage,
        "kind": rec.kind,
        "category": rec.category,
        "platforms": list(rec.platforms),
        "tags": list(rec.tags),
        "capabilities": list(rec.capabilities),
        "dependencies": json.loads(json.dumps(rec.dependencies, sort_keys=True)),
        "entrypoints": json.loads(json.dumps(rec.entrypoints, sort_keys=True)),
        "payload": json.loads(json.dumps(rec.payload, sort_keys=True)),
        "unknown_fields": sorted(rec.unknown_fields),
    }


# ── Shared envelope checks ────────────────────────────────────────────────


def _check_version(version: Any, ref: str, findings: List[Finding], *, required: bool = True) -> str:
    if version in (None, ""):
        if required:
            findings.append(Finding(ERROR, "missing-version", ref, "no version declared"))
        return ""
    text = str(version)
    if not _VERSION_RE.match(text):
        findings.append(
            Finding(ERROR, "invalid-version", ref, f"version {text!r} is not semver (X.Y.Z)")
        )
    return text


def _check_platforms(raw: Any, ref: str, findings: List[Finding]) -> List[str]:
    if raw in (None, ""):
        return []
    values = raw if isinstance(raw, list) else [raw]
    out: List[str] = []
    for item in values:
        token = str(item).strip().lower()
        out.append(token)
        if token not in KNOWN_PLATFORMS:
            findings.append(
                Finding(
                    ERROR,
                    "unknown-platform",
                    ref,
                    f"platform {token!r} not one of {sorted(KNOWN_PLATFORMS)}",
                )
            )
    return out


def _mapping(value: Any) -> Mapping[str, Any]:
    """Return *value* when it is a mapping, else an empty mapping.

    Manifests are user-authored: any nested block may be absent or the
    wrong shape, and every such case degrades to "no declarations" rather
    than raising.
    """
    return value if isinstance(value, Mapping) else {}


def _str_list(raw: Any) -> List[str]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raw = [raw]
    return [str(x) for x in raw]


def _safe_relative_path(value: Any) -> Optional[str]:
    """Return the value when it is a safe package-relative path, else None.

    Mirrors hermes_cli.web_server._safe_plugin_api_relpath semantics:
    relative, no drive/root anchor, no ``..`` traversal segments.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("\\", "/")
    if text.startswith("/") or re.match(r"^[A-Za-z]:", text):
        return None
    parts = [p for p in text.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        return None
    return "/".join(parts) if parts else None


def _check_manifest_paths(
    ref: str, findings: List[Finding], **paths: Any
) -> Dict[str, Optional[str]]:
    out: Dict[str, Optional[str]] = {}
    for label, raw in sorted(paths.items()):
        if raw is None:
            out[label] = None
            continue
        safe = _safe_relative_path(raw)
        if safe is None:
            findings.append(
                Finding(
                    ERROR,
                    "unsafe-path",
                    ref,
                    f"{label} path {raw!r} must be a relative path inside the "
                    "package (no absolute paths, no '..')",
                )
            )
        out[label] = safe
    return out


# ── Plugin family ─────────────────────────────────────────────────────────


def normalize_env_declarations(
    raw: Any, ref: str, findings: List[Finding], *, field_name: str
) -> Tuple[List[str], List[Dict[str, Any]]]:
    """Normalize requires_env / optional_env (both documented shapes).

    Simple shape: ``["API_KEY"]``. Rich shape: ``[{name, description?,
    prompt?, url?, password?}]``. Returns (names, rich_entries).
    """
    if raw is None:
        return [], []
    names: List[str] = []
    rich: List[Dict[str, Any]] = []
    if not isinstance(raw, list):
        findings.append(
            Finding(ERROR, "invalid-env-declaration", ref, f"{field_name} must be a list")
        )
        return [], []
    for item in raw:
        if isinstance(item, str) and item.strip():
            names.append(item.strip())
        elif isinstance(item, Mapping) and str(item.get("name") or "").strip():
            entry = {k: item[k] for k in sorted(item)}
            names.append(str(item["name"]).strip())
            rich.append(entry)
        else:
            findings.append(
                Finding(
                    ERROR,
                    "invalid-env-declaration",
                    ref,
                    f"{field_name} entry {item!r} must be an env-var name or a "
                    "{name, ...} mapping",
                )
            )
    return names, rich


def normalize_plugin_requires(raw: Any, ref: str, findings: List[Finding]) -> List[Dict[str, Any]]:
    """Normalize requires_plugins to [{id, version_range}] (loader semantics)."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        findings.append(
            Finding(WARNING, "invalid-dependency-declaration", ref, "requires_plugins must be a list")
        )
        return []
    deps: List[Dict[str, Any]] = []
    for item in raw:
        if isinstance(item, str) and item.strip():
            deps.append({"id": item.strip(), "version_range": None})
        elif isinstance(item, Mapping) and isinstance(item.get("id"), str) and item["id"]:
            vr = item.get("version_range")
            deps.append({"id": item["id"], "version_range": str(vr) if vr is not None else None})
        else:
            findings.append(
                Finding(
                    WARNING,
                    "invalid-dependency-declaration",
                    ref,
                    f"requires_plugins entry {item!r} must be a plugin id string "
                    "or a {id, version_range} mapping",
                )
            )
    return deps


def _known_capability_ids() -> frozenset:
    try:
        from hermes_cli.plugin_capabilities import CAPABILITY_REGISTRY

        return frozenset(CAPABILITY_REGISTRY.keys())
    except Exception:  # pragma: no cover — capabilities module unavailable
        return frozenset()


def parse_plugin_manifest(
    data: Mapping, *, key: str, path: str, ownership: str = "bundled"
) -> Tuple[PackageRecord, List[Finding]]:
    """Normalize one plugin.yaml mapping into a PackageRecord + findings.

    Pure and side-effect free; the runtime loader
    (hermes_cli.plugins.PluginManager) keeps its own warn-and-continue
    logging while this function is the contract's source of truth for
    field semantics.
    """
    findings: List[Finding] = []
    name = str(data.get("name") or "")
    ref = f"plugin:{key}"
    if not name:
        findings.append(Finding(ERROR, "missing-name", ref, "no name declared"))
    if not str(data.get("description") or "").strip():
        findings.append(Finding(ERROR, "missing-description", ref, "no description declared"))

    raw_mv = data.get("manifest_version", 1)
    try:
        mv = int(raw_mv)
    except (TypeError, ValueError):
        findings.append(
            Finding(WARNING, "manifest-version-invalid", ref,
                    f"manifest_version {raw_mv!r} is not an integer; treated as 1")
        )
        mv = 1
    if mv > PLUGIN_MANIFEST_SUPPORTED:
        findings.append(
            Finding(WARNING, "manifest-version-newer", ref,
                    f"manifest_version {mv} is newer than supported "
                    f"({PLUGIN_MANIFEST_SUPPORTED}); unknown fields are ignored")
        )

    raw_kind = data.get("kind", "standalone")
    kind = str(raw_kind).strip().lower() if isinstance(raw_kind, str) else "standalone"
    if kind not in VALID_PLUGIN_KINDS:
        findings.append(
            Finding(WARNING, "unknown-kind", ref,
                    f"kind {raw_kind!r} not one of {sorted(VALID_PLUGIN_KINDS)}; "
                    "loader treats it as 'standalone'")
        )
        kind = "standalone"

    category = key.rsplit("/", 1)[0] if "/" in key else ""
    expected_kind = PLUGIN_CATEGORY_KINDS.get(category)
    if expected_kind and kind != expected_kind:
        findings.append(
            Finding(
                ERROR,
                "kind-family-mismatch",
                ref,
                f"packages under plugins/{category}/ are routed as "
                f"kind={expected_kind!r} by their discovery system, but the "
                f"manifest declares {data.get('kind', '(absent → standalone)')!r}",
            )
        )

    if not str(data.get("author") or "").strip():
        findings.append(Finding(WARNING, "missing-author", ref, "no author declared"))

    version = _check_version(data.get("version"), ref, findings)
    platforms = _check_platforms(data.get("platforms"), ref, findings)

    env_names, env_rich = normalize_env_declarations(
        data.get("requires_env"), ref, findings, field_name="requires_env"
    )
    opt_names, opt_rich = normalize_env_declarations(
        data.get("optional_env"), ref, findings, field_name="optional_env"
    )
    requires_plugins = normalize_plugin_requires(data.get("requires_plugins"), ref, findings)

    # ``python_dependencies`` (v2) is the current spelling; ``pip_dependencies``
    # (v1) is a permanent compatibility alias. Declaring the v2 key — even as an
    # empty list — is explicit and wins. Both are declare-only: Hermes surfaces
    # them and never auto-installs.
    raw_pydeps = data.get("python_dependencies")
    if raw_pydeps is None:
        raw_pydeps = data.get("pip_dependencies")
    if raw_pydeps is not None and not isinstance(raw_pydeps, list):
        findings.append(
            Finding(WARNING, "invalid-dependency-declaration", ref,
                    "python_dependencies/pip_dependencies must be a list of "
                    "requirement strings; ignoring")
        )
        raw_pydeps = None
    python_deps = [
        str(x).strip() for x in (raw_pydeps or []) if str(x).strip()
    ]

    capabilities = _str_list(data.get("capabilities"))
    known_caps = _known_capability_ids()
    for cap in capabilities:
        if known_caps and cap not in known_caps:
            findings.append(
                Finding(WARNING, "unknown-capability", ref,
                        f"capability {cap!r} is not in the capability registry")
            )

    unknown = sorted(set(data.keys()) - set(KNOWN_PLUGIN_MANIFEST_FIELDS))
    for fname in unknown:
        findings.append(
            Finding(WARNING, "unknown-field", ref,
                    f"unknown manifest field {fname!r} (ignored by loaders)")
        )

    rec = PackageRecord(
        family="plugin",
        id=key,
        path=path,
        manifest_path=f"{path}/plugin.yaml",
        ownership=ownership,
        schema_version=mv,
        name=name,
        version=version,
        description=str(data.get("description") or ""),
        author=str(data.get("author") or ""),
        license=str(data.get("license") or ""),
        homepage=str(data.get("homepage") or ""),
        kind=kind,
        category=category,
        platforms=platforms,
        tags=_str_list(data.get("tags")),
        capabilities=capabilities,
        dependencies={
            "python": python_deps,
            "plugins": requires_plugins,
            "env": env_names,
            "env_rich": env_rich,
            "optional_env": opt_names,
            "optional_env_rich": opt_rich,
            "commands": [],
            "external": _str_list(data.get("external_dependencies")),
        },
        entrypoints={
            "register": True,  # refined by the enumerator against the tree
            "hooks": _str_list(data.get("hooks")),
        },
        payload={
            "label": str(data.get("label") or ""),
            "api_version": data.get("api_version"),
            "provides_tools": _str_list(data.get("provides_tools")),
            "provides_hooks": _str_list(data.get("provides_hooks")),
            "provides_web_providers": _str_list(data.get("provides_web_providers")),
            "provides_browser_providers": _str_list(data.get("provides_browser_providers")),
            "config_schema": dict(_mapping(data.get("config_schema"))),
            "emits": _str_list(data.get("emits")),
            "listens": _str_list(data.get("listens")),
        },
        unknown_fields=unknown,
    )
    return rec, findings


# ── Skill family ──────────────────────────────────────────────────────────

#: Description hardline (skill authoring standards rule 1) — the same
#: 60-char system-prompt budget the loader truncates at.
SKILL_DESCRIPTION_LIMIT = SKILL_PROMPT_DESC_LIMIT


def parse_skill_frontmatter(
    fm: Mapping, *, skill_dir: str, tier: str, has_body: bool = True
) -> Tuple[PackageRecord, List[Finding]]:
    """Normalize one SKILL.md frontmatter mapping into a PackageRecord."""
    findings: List[Finding] = []
    dir_path = skill_dir.rstrip("/")
    dir_name = dir_path.rsplit("/", 1)[-1]
    name = str(fm.get("name") or "")
    skill_id = name or dir_name
    ref = f"skill:{skill_id}"

    if not name:
        findings.append(Finding(ERROR, "missing-name", ref, "no name declared"))
    elif name != dir_name:
        findings.append(
            Finding(ERROR, "name-dir-mismatch", ref,
                    f"frontmatter name {name!r} != directory {dir_name!r}")
        )

    desc = str(fm.get("description") or "")
    if not desc.strip():
        findings.append(Finding(ERROR, "missing-description", ref, "no description declared"))
    elif len(desc) > SKILL_DESCRIPTION_LIMIT:
        findings.append(
            Finding(ERROR, "description-too-long", ref,
                    f"description is {len(desc)} chars (hardline "
                    f"{SKILL_DESCRIPTION_LIMIT})")
        )

    for required in ("version", "author", "license", "platforms"):
        if required not in fm:
            findings.append(
                Finding(ERROR, f"missing-{required}", ref, f"no {required} declared")
            )

    version = _check_version(fm.get("version"), ref, findings, required=False)
    platforms = _check_platforms(fm.get("platforms"), ref, findings)

    hermes = dict(_mapping(_mapping(fm.get("metadata")).get("hermes")))

    # Top-level tags/category are accepted and mirrored by the loader.
    tags = _str_list(hermes.get("tags") if hermes.get("tags") is not None else fm.get("tags"))
    category = str(
        hermes.get("category")
        if hermes.get("category") is not None
        else (fm.get("category") or "")
    )

    prereqs = _mapping(fm.get("prerequisites"))
    # required_environment_variables accepts both plain names and rich
    # {name, prompt, help, required_for, optional} entries — mirror
    # tools/skills_tool.py::_get_required_environment_variables.
    req_env_raw = fm.get("required_environment_variables")
    if isinstance(req_env_raw, Mapping):
        req_env_raw = [req_env_raw]
    req_env_names: List[str] = []
    req_env_rich: List[Dict[str, Any]] = []
    for item in req_env_raw if isinstance(req_env_raw, list) else []:
        if isinstance(item, str) and item.strip():
            req_env_names.append(item.strip())
        elif isinstance(item, Mapping) and str(item.get("name") or "").strip():
            req_env_names.append(str(item["name"]).strip())
            req_env_rich.append({k: item[k] for k in sorted(item)})
        else:
            findings.append(
                Finding(ERROR, "invalid-env-declaration", ref,
                        f"required_environment_variables entry {item!r} must "
                        "be an env-var name or a {name, ...} mapping")
            )
    env_vars = _str_list(prereqs.get("env_vars")) + req_env_names
    # De-dup, preserving declaration order.
    env_vars = list(dict.fromkeys(env_vars))
    commands = _str_list(prereqs.get("commands"))

    unknown = sorted(set(fm.keys()) - set(KNOWN_SKILL_FRONTMATTER_FIELDS))
    for fname in unknown:
        findings.append(
            Finding(WARNING, "unknown-field", ref,
                    f"unknown frontmatter field {fname!r} (no consumer reads it)")
        )
    unknown_hermes = sorted(set(hermes.keys()) - set(KNOWN_SKILL_HERMES_KEYS))
    for fname in unknown_hermes:
        findings.append(
            Finding(WARNING, "unknown-metadata", ref,
                    f"unknown metadata.hermes key {fname!r}")
        )

    if not has_body:
        findings.append(
            Finding(ERROR, "unresolved-entrypoint", ref,
                    "SKILL.md has no body after the frontmatter")
        )

    rec = PackageRecord(
        family="skill",
        id=skill_id,
        path=dir_path,
        manifest_path=f"{dir_path}/SKILL.md",
        ownership=tier,
        schema_version=SKILL_SCHEMA_SUPPORTED,
        name=name,
        version=version,
        description=desc,
        author=str(fm.get("author") or ""),
        license=str(fm.get("license") or ""),
        homepage=str(hermes.get("homepage") or ""),
        kind="",
        category=category,
        platforms=platforms,
        tags=tags,
        capabilities=[],
        dependencies={
            "python": _str_list(fm.get("dependencies")),
            "plugins": [],
            "env": env_vars,
            "env_rich": req_env_rich,
            "optional_env": [],
            "optional_env_rich": [],
            "commands": commands,
            "external": [],
        },
        entrypoints={"body": bool(has_body)},
        payload={
            "related_skills": _str_list(hermes.get("related_skills")),
            "requires_toolsets": _str_list(hermes.get("requires_toolsets")),
            "fallback_for_toolsets": _str_list(hermes.get("fallback_for_toolsets")),
            "requires_tools": _str_list(hermes.get("requires_tools")),
            "fallback_for_tools": _str_list(hermes.get("fallback_for_tools")),
            "config": hermes.get("config") or [],
            "environments": _str_list(fm.get("environments")),
            "setup": dict(_mapping(fm.get("setup"))),
            "required_credential_files": _str_list(fm.get("required_credential_files")),
            "compatibility": str(fm.get("compatibility") or ""),
        },
        unknown_fields=unknown + [f"metadata.hermes.{k}" for k in unknown_hermes],
    )
    return rec, findings


# ── MCP family ────────────────────────────────────────────────────────────


def parse_mcp_manifest(
    data: Mapping, *, name: str, path: str
) -> Tuple[PackageRecord, List[Finding]]:
    """Normalize one optional-mcps manifest.yaml into a PackageRecord.

    The install-time reader (hermes_cli.mcp_catalog._read_manifest) stays
    fail-closed; this normalization mirrors its field semantics for
    inventory/lint purposes.
    """
    findings: List[Finding] = []
    ref = f"mcp:{name}"

    raw_mv = data.get("manifest_version")
    if isinstance(raw_mv, bool) or not isinstance(raw_mv, int):
        findings.append(
            Finding(ERROR, "manifest-version-invalid", ref,
                    f"manifest_version {raw_mv!r} is not an integer")
        )
        mv = 0
    else:
        mv = raw_mv
        if mv != MCP_MANIFEST_SUPPORTED:
            findings.append(
                Finding(ERROR, "manifest-version-unsupported", ref,
                        f"manifest_version {mv} != supported "
                        f"{MCP_MANIFEST_SUPPORTED}")
            )

    declared = str(data.get("name") or "")
    if not declared:
        findings.append(Finding(ERROR, "missing-name", ref, "no name declared"))
    elif declared != name:
        findings.append(
            Finding(ERROR, "name-dir-mismatch", ref,
                    f"manifest name {declared!r} != directory {name!r}")
        )
    if not str(data.get("description") or "").strip():
        findings.append(Finding(ERROR, "missing-description", ref, "no description declared"))

    transport = _mapping(data.get("transport"))
    ttype = str(transport.get("type") or "")
    command = transport.get("command")
    url = transport.get("url")
    if ttype == "stdio" and not command:
        findings.append(
            Finding(ERROR, "unresolved-entrypoint", ref, "stdio transport with no command")
        )
    elif ttype == "http" and not url:
        findings.append(
            Finding(ERROR, "unresolved-entrypoint", ref, "http transport with no url")
        )
    elif ttype not in ("stdio", "http"):
        findings.append(
            Finding(ERROR, "unresolved-entrypoint", ref,
                    f"transport type {ttype!r} is not stdio|http")
        )

    unknown = sorted(set(data.keys()) - set(KNOWN_MCP_MANIFEST_FIELDS))
    for fname in unknown:
        findings.append(
            Finding(WARNING, "unknown-field", ref, f"unknown manifest field {fname!r}")
        )

    auth = _mapping(data.get("auth"))
    rec = PackageRecord(
        family="mcp",
        id=name,
        path=path,
        manifest_path=f"{path}/manifest.yaml",
        ownership="optional",
        schema_version=mv,
        name=declared,
        version=str(transport.get("version") or ""),
        description=str(data.get("description") or ""),
        author="Nous Research",  # catalog policy: presence == Nous approval
        license="",
        homepage=str(data.get("source") or ""),
        kind=ttype,
        category="",
        platforms=[],
        tags=[],
        capabilities=[],
        dependencies={
            "python": [], "plugins": [],
            "env": [
                str(e.get("name"))
                for e in (auth.get("env") or [])
                if isinstance(e, Mapping) and e.get("name")
            ],
            "env_rich": [], "optional_env": [], "optional_env_rich": [],
            "commands": [], "external": [],
        },
        entrypoints={"transport": ttype, "command": command, "url": url},
        payload={
            "auth_type": str(auth.get("type") or ""),
            "post_install": bool(data.get("post_install")),
            "install": dict(_mapping(data.get("install"))),
        },
        unknown_fields=unknown,
    )
    return rec, findings


# ── Dashboard payload ─────────────────────────────────────────────────────


def parse_dashboard_manifest(
    data: Mapping, *, key: str, path: str, ownership: str = "bundled"
) -> Tuple[PackageRecord, List[Finding]]:
    """Normalize one dashboard/manifest.json into a PackageRecord.

    Used for dashboard-app packages (a plugins/<name>/ directory whose only
    manifest is dashboard/manifest.json — e.g. kanban). When a package has
    a plugin.yaml too, the dashboard manifest is validated as a payload of
    that package instead of producing a second record.
    """
    findings: List[Finding] = []
    ref = f"plugin:{key}"
    name = str(data.get("name") or "")
    if not name:
        findings.append(Finding(ERROR, "missing-name", ref, "no name declared"))
    version = _check_version(data.get("version"), ref, findings)

    paths = _check_manifest_paths(
        ref,
        findings,
        entry=data.get("entry", "dist/index.js"),
        css=data.get("css"),
        api=data.get("api"),
    )

    unknown = sorted(set(data.keys()) - set(KNOWN_DASHBOARD_MANIFEST_FIELDS))
    for fname in unknown:
        findings.append(
            Finding(WARNING, "unknown-field", ref, f"unknown manifest field {fname!r}")
        )

    tab = _mapping(data.get("tab"))
    rec = PackageRecord(
        family="plugin",
        id=key,
        path=path,
        manifest_path=f"{path}/dashboard/manifest.json",
        ownership=ownership,
        schema_version=1,
        name=name,
        version=version,
        description=str(data.get("description") or ""),
        author="",
        license="",
        homepage="",
        kind="dashboard",
        category="",
        platforms=[],
        tags=[],
        capabilities=[],
        dependencies={
            "python": [], "plugins": [], "env": [], "env_rich": [],
            "optional_env": [], "optional_env_rich": [], "commands": [],
            "external": [],
        },
        entrypoints={
            "entry": paths.get("entry"),
            "css": paths.get("css"),
            "api": paths.get("api"),
        },
        payload={
            "label": str(data.get("label") or name),
            "icon": str(data.get("icon") or "Puzzle"),
            "tab": {k: tab[k] for k in sorted(tab)},
            "slots": _str_list(data.get("slots")),
        },
        unknown_fields=unknown,
    )
    return rec, findings


# ── Cross-package validation ──────────────────────────────────────────────


def validate_cross_package(records: Sequence[PackageRecord]) -> List[Finding]:
    """Repo-scope rules: duplicate identity, unresolved inter-package deps.

    Skill identity is global across tiers (a bundled and an optional skill
    with the same name collide at install time — ~/.hermes/skills/<name>).
    Plugin identity is the path-derived key. MCP identity is the catalog
    directory name.
    """
    findings: List[Finding] = []

    seen: Dict[Tuple[str, str], PackageRecord] = {}
    for rec in records:
        dup_key = (rec.family, rec.id)
        if dup_key in seen:
            findings.append(
                Finding(
                    ERROR,
                    "duplicate-id",
                    rec.ref,
                    f"identity already used by {seen[dup_key].path} "
                    f"(this: {rec.path})",
                )
            )
        else:
            seen[dup_key] = rec

    plugin_ids = {r.id for r in records if r.family == "plugin"}
    plugin_names = {r.name for r in records if r.family == "plugin"}
    for rec in records:
        if rec.family != "plugin":
            continue
        for dep in rec.dependencies.get("plugins", []):
            dep_id = dep.get("id")
            if dep_id and dep_id not in plugin_ids and dep_id not in plugin_names:
                findings.append(
                    Finding(
                        WARNING,
                        "unresolved-dependency",
                        rec.ref,
                        f"requires_plugins {dep_id!r} does not resolve to a "
                        "source-owned plugin (advisory; user-installed "
                        "plugins can satisfy it at runtime)",
                    )
                )

    skill_names = {r.id for r in records if r.family == "skill"}
    for rec in records:
        if rec.family != "skill":
            continue
        for rel in rec.payload.get("related_skills", []):
            if rel not in skill_names:
                findings.append(
                    Finding(
                        WARNING,
                        "unresolved-dependency",
                        rec.ref,
                        f"related_skills {rel!r} does not resolve to a "
                        "source-owned skill",
                    )
                )

    findings.sort(key=lambda f: (f.package, f.rule, f.message))
    return findings


# ── Enumerator ────────────────────────────────────────────────────────────

#: Non-package infrastructure under the family roots. Explicit and
#: documented — anything new and unclassified is an orphan finding, never a
#: silent skip. Paths are repo-relative.
KNOWN_INFRASTRUCTURE = frozenset(
    {
        "plugins/__init__.py",
        "plugins/plugin_utils.py",
        # Context-engine registry module — ABC + orchestration, no packages yet.
        "plugins/context_engine",
        # Shared per-family framework modules (not packages).
        "plugins/memory/__init__.py",
        "plugins/memory/config_schema.py",
        "plugins/memory/query_rewrite.py",
        "plugins/cron_providers/__init__.py",
        "plugins/web/__init__.py",
        # Skill hub index cache — data, not a package.
        "skills/index-cache",
    }
)

_SKILL_EXCLUDED_DIRS = frozenset({".git", "__pycache__", ".archive", "index-cache"})


def _has_source_content(directory: Path) -> bool:
    """True when *directory*'s subtree holds any file a package could own.

    Build residue is not a package and must not be reported as an orphan:
    ``__pycache__`` and other cache directories, plus the empty
    directories git leaves behind when tracked files move away, survive a
    branch switch and would otherwise turn a clean tree's inventory red
    for reasons no author can act on. Anything else — even a stray README
    — is real content and stays classifiable, so a genuinely malformed
    package still reports ``orphan-package``.
    """
    try:
        entries = sorted(directory.iterdir(), key=lambda c: c.name)
    except OSError:
        return False
    for entry in entries:
        if entry.name.startswith((".", "__")):
            continue
        if entry.is_file():
            return True
        if entry.name in _SKILL_EXCLUDED_DIRS:
            continue
        if _has_source_content(entry):
            return True
    return False


def _iter_dirs(path: Path) -> List[Path]:
    if not path.is_dir():
        return []
    return sorted(
        (c for c in path.iterdir() if c.is_dir() and not c.name.startswith((".", "__"))),
        key=lambda p: p.name,
    )


def _load_yaml_file(path: Path) -> Optional[Mapping]:
    try:
        data = yaml_load(path.read_text(encoding="utf-8"))
        return data if isinstance(data, Mapping) else None
    except Exception:
        return None


def _plugin_package_dirs(plugins_root: Path) -> Tuple[List[Tuple[Path, str]], List[Path]]:
    """Return ([(package_dir, key)], [orphan_dirs]) under plugins/."""
    packages: List[Tuple[Path, str]] = []
    orphans: List[Path] = []
    for child in _iter_dirs(plugins_root):
        rel = f"plugins/{child.name}"
        if not _has_source_content(child):
            continue
        if (child / "plugin.yaml").exists():
            packages.append((child, child.name))
            continue
        if (child / "dashboard" / "manifest.json").exists():
            packages.append((child, child.name))
            continue
        # Category directory: every subdirectory must be a package.
        subpkgs = [
            (sub, f"{child.name}/{sub.name}")
            for sub in _iter_dirs(child)
            if (sub / "plugin.yaml").exists()
            or (sub / "dashboard" / "manifest.json").exists()
        ]
        if subpkgs:
            packages.extend(subpkgs)
            known_names = {p.name for p, _ in subpkgs}
            for sub in _iter_dirs(child):
                if sub.name not in known_names and _has_source_content(sub):
                    orphans.append(sub)
            continue
        if rel in KNOWN_INFRASTRUCTURE:
            continue
        orphans.append(child)
    return packages, orphans


def _skill_package_dirs(root: Path, rel_root: str) -> Tuple[List[Path], List[Path]]:
    """Return ([skill_dirs], [orphan_dirs]) under a skills tree."""
    skills: List[Path] = []
    orphans: List[Path] = []

    def walk(directory: Path) -> bool:
        """DFS; returns True when the subtree is classified.

        A directory is classified when it is a skill package (has
        SKILL.md), a category descriptor (has DESCRIPTION.md — empty
        categories are legitimate), or every child directory classifies.
        """
        if (directory / "SKILL.md").exists():
            skills.append(directory)
            return True
        found = (directory / "DESCRIPTION.md").exists()
        for child in _iter_dirs(directory):
            if child.name in _SKILL_EXCLUDED_DIRS:
                continue
            if not _has_source_content(child):
                continue
            rel = f"{rel_root}/{child.relative_to(root)}".replace("\\", "/")
            if rel in KNOWN_INFRASTRUCTURE:
                found = True
                continue
            if walk(child):
                found = True
            else:
                orphans.append(child)
        return found

    for child in _iter_dirs(root):
        if child.name in _SKILL_EXCLUDED_DIRS:
            continue
        if not _has_source_content(child):
            continue
        rel = f"{rel_root}/{child.name}"
        if rel in KNOWN_INFRASTRUCTURE:
            continue
        if not walk(child):
            orphans.append(child)
    return skills, orphans


def enumerate_source_packages(
    repo_root: Path | str,
) -> Tuple[List[PackageRecord], List[Finding]]:
    """Classify every source-owned package under *repo_root* exactly once.

    Returns deterministic (records, findings): records sorted by
    (family, id); findings sorted by (package, rule, message). Orphan
    directories — candidates under a family root that match no
    classification rule and are not documented infrastructure — produce an
    ``orphan-package`` error.
    """
    root = Path(repo_root)
    records: List[PackageRecord] = []
    findings: List[Finding] = []

    def rel(p: Path) -> str:
        return str(p.relative_to(root)).replace("\\", "/")

    # ── plugins ──
    packages, orphans = _plugin_package_dirs(root / "plugins")
    for pkg_dir, key in packages:
        manifest = pkg_dir / "plugin.yaml"
        dash_manifest = pkg_dir / "dashboard" / "manifest.json"
        if manifest.exists():
            data = _load_yaml_file(manifest)
            if data is None:
                findings.append(
                    Finding(ERROR, "manifest-unreadable", f"plugin:{key}",
                            f"{rel(manifest)} is not a YAML mapping")
                )
                continue
            rec, recf = parse_plugin_manifest(data, key=key, path=rel(pkg_dir))
            # Entrypoint resolution against the actual tree.
            init_file = pkg_dir / "__init__.py"
            if not init_file.exists():
                findings.append(
                    Finding(ERROR, "unresolved-entrypoint", rec.ref,
                            "package has no __init__.py")
                )
                rec.entrypoints["register"] = False
            elif rec.kind in ("standalone", "backend"):
                try:
                    has_register = "def register" in init_file.read_text(
                        encoding="utf-8", errors="replace"
                    )
                except OSError:
                    has_register = False
                rec.entrypoints["register"] = has_register
                if not has_register:
                    findings.append(
                        Finding(ERROR, "unresolved-entrypoint", rec.ref,
                                "__init__.py defines no register(ctx)")
                    )
            if dash_manifest.exists():
                # Dashboard payload of the same package — validate, don't
                # double-count.
                dash_data = _load_json_file(dash_manifest)
                if dash_data is None:
                    findings.append(
                        Finding(ERROR, "manifest-unreadable", rec.ref,
                                f"{rel(dash_manifest)} is not a JSON object")
                    )
                else:
                    _, dashf = parse_dashboard_manifest(
                        dash_data, key=key, path=rel(pkg_dir)
                    )
                    findings.extend(dashf)
                    rec.payload["dashboard"] = True
            records.append(rec)
            findings.extend(recf)
        else:
            data = _load_json_file(dash_manifest)
            if data is None:
                findings.append(
                    Finding(ERROR, "manifest-unreadable", f"plugin:{key}",
                            f"{rel(dash_manifest)} is not a JSON object")
                )
                continue
            rec, recf = parse_dashboard_manifest(data, key=key, path=rel(pkg_dir))
            entry = rec.entrypoints.get("entry")
            if entry and not (pkg_dir / "dashboard" / entry).exists():
                findings.append(
                    Finding(ERROR, "unresolved-entrypoint", rec.ref,
                            f"dashboard entry {entry!r} does not exist")
                )
            records.append(rec)
            findings.extend(recf)
    for orphan in orphans:
        findings.append(
            Finding(ERROR, "orphan-package", rel(orphan),
                    "directory under plugins/ is neither a package, a "
                    "category of packages, nor documented infrastructure")
        )

    # ── skills ──
    for tree, tier in (("skills", "bundled"), ("optional-skills", "optional")):
        troot = root / tree
        if not troot.is_dir():
            continue
        skill_dirs, orphans = _skill_package_dirs(troot, tree)
        for sdir in sorted(skill_dirs, key=rel):
            fm, body = parse_frontmatter(
                (sdir / "SKILL.md").read_text(encoding="utf-8")
            )
            rec, recf = parse_skill_frontmatter(
                fm,
                skill_dir=rel(sdir),
                tier=tier,
                has_body=bool(body.strip()),
            )
            rec.category = rec.category or rel(sdir.parent).split("/", 1)[-1]
            records.append(rec)
            findings.extend(recf)
        for orphan in orphans:
            findings.append(
                Finding(ERROR, "orphan-package", rel(orphan),
                        f"directory under {tree}/ contains no SKILL.md and is "
                        "not documented infrastructure")
            )

    # ── mcp catalog ──
    mcp_root = root / "optional-mcps"
    for child in _iter_dirs(mcp_root):
        manifest = child / "manifest.yaml"
        if not manifest.exists():
            findings.append(
                Finding(ERROR, "orphan-package", rel(child),
                        "directory under optional-mcps/ has no manifest.yaml")
            )
            continue
        data = _load_yaml_file(manifest)
        if data is None:
            findings.append(
                Finding(ERROR, "manifest-unreadable", f"mcp:{child.name}",
                        f"{rel(manifest)} is not a YAML mapping")
            )
            continue
        rec, recf = parse_mcp_manifest(data, name=child.name, path=rel(child))
        records.append(rec)
        findings.extend(recf)

    records.sort(key=lambda r: (r.family, r.id, r.path))
    findings.extend(validate_cross_package(records))
    findings.sort(key=lambda f: (f.package, f.rule, f.message))
    return records, findings


def _load_json_file(path: Path) -> Optional[Mapping]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, Mapping) else None
    except Exception:
        return None
