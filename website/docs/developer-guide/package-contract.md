# Package Contract

Hermes ships three families of source-owned packages — native plugins,
skills, and curated MCP catalog entries — plus dashboard-app extensions.
The **package contract** is the single, versioned definition of what a
package manifest means across all of them: one common envelope, four
family payloads, one canonical parser/validator library, and one
deterministic CLI.

- **Library:** `agent/package_contract.py` (`CONTRACT_VERSION = 1`)
- **CLI:** `hermes packages inventory | lint | migrate`
- **CI gate:** `tests/test_package_inventory.py` (zero findings for every
  source-owned package) and `tests/test_package_contract.py` (rule
  coverage)

## Families and manifests

| Family | Manifest | Identity (public ID) | Schema version |
| --- | --- | --- | --- |
| Native plugin | `plugins/**/plugin.yaml` | path-derived key (`disk-cleanup`, `image_gen/openai`) | `manifest_version` — absent = 1 (supported forever), current = 2 |
| Dashboard-app plugin | `plugins/<name>/dashboard/manifest.json` | directory name (`kanban`) | 1 (implicit) |
| Skill | `skills/**/SKILL.md`, `optional-skills/**/SKILL.md` frontmatter | skill name == directory name | 1 (defined by this contract; not declared in frontmatter) |
| MCP catalog entry | `optional-mcps/<name>/manifest.yaml` | directory name | `manifest_version: 1` |

A package may carry **both** a `plugin.yaml` and a `dashboard/manifest.json`
— the dashboard manifest is a payload of the same package, never a second
package.

Versioning stays **per family**: the contract deliberately does not add a
monolithic cross-family version literal to any manifest (see the
[native plugin compatibility contract](plugins/index.md#native-plugin-compatibility-contract) —
version only what crosses a persisted boundary, which each family's
manifest schema already does).

## The common envelope

Every family's manifest normalizes into the same envelope
(`PackageRecord`):

| Envelope field | plugin.yaml | SKILL.md frontmatter | mcp manifest.yaml | dashboard manifest.json |
| --- | --- | --- | --- | --- |
| identity | `name` + path-derived key | `name` (== dir) | `name` (== dir) | `name` |
| version | `version` (semver) | `version` (semver) | `transport.version` (pin, optional) | `version` |
| description | `description` | `description` (≤ 60 chars, ends with `.`) | `description` | `description` |
| provenance | `author`, `license`, `homepage` | `author`, `license`, `metadata.hermes.homepage` / `upstream` / `credits` / `supersedes` / `related_docs` | `source` (catalog policy: presence = Nous approval) | — |
| platform | `platforms` (optional) | `platforms` (required) | — | — |
| capability | `kind`, `capabilities`, `provides_*`, `hooks` | `metadata.hermes.requires_toolsets` / `fallback_for_*` / `session_platforms` | `transport.type`, `auth.type`, `suggest` | `tab`, `slots` |
| dependencies | `pip_dependencies` / `python_dependencies`, `requires_plugins`, `requires_env` / `optional_env`, `external_dependencies` | `dependencies` (pip, declared-only), `prerequisites.{env_vars,commands}`, `required_environment_variables`, `required_credential_files` | `auth.env`, `install` | — |
| entrypoint | `__init__.py` `register(ctx)` (standalone/backend); family discovery otherwise | SKILL.md body | `transport.command` / `transport.url` | `entry` / `css` / `api` (relative, inside `dashboard/`) |

Platform tokens: `linux`, `macos`, `windows`, `darwin`, `termux`,
`android` (`KNOWN_PLATFORMS`). Plugin kinds: `standalone`, `backend`,
`exclusive`, `platform`, `model-provider` (`VALID_PLUGIN_KINDS`), plus the
contract-level `dashboard` for dashboard-app packages. Nested category
directories imply a kind (`PLUGIN_CATEGORY_KINDS`): `memory` and
`cron_providers` are `exclusive`, `model-providers` is `model-provider`,
`platforms` is `platform`, and `browser`/`dashboard_auth`/`image_gen`/
`video_gen`/`web` are `backend`; a manifest that contradicts its
category's routing is a `kind-family-mismatch` error.

## Validation rules

`hermes packages lint` (and the CI inventory test) enforce, per package:

| Rule | Severity | Meaning |
| --- | --- | --- |
| `missing-name` / `missing-description` / `missing-version` | error | envelope identity incomplete |
| `name-dir-mismatch` | error | declared name must equal the directory-derived identity |
| `invalid-version` | error | version is not semver (`X.Y.Z[-pre][+build]`) |
| `description-too-long` | error (skills) | breaks the 60-char system-prompt budget |
| `missing-author` / `missing-license` / `missing-platforms` | warning (plugins) / error (skills) | provenance/platform gaps |
| `duplicate-id` | error | identity collision within a family (skill names are global across tiers; plugin `name` may repeat across categories because the key is the identity) |
| `orphan-package` | error | a directory under a family root that is neither a package, a category, nor documented infrastructure (`KNOWN_INFRASTRUCTURE`). Build residue — a directory whose whole subtree is caches (`__pycache__`) or the empty directories git leaves when tracked files move away — is skipped, not reported: it is not something an author can fix, and a fresh CI checkout would not see it. A directory with any real content still reports. |
| `unknown-field` / `unknown-metadata` | warning | field no consumer reads (forward-compat surface at runtime; zero-tolerance for source-owned packages in CI) |
| `unsafe-path` | error | manifest path escaping the package (absolute, drive letter, `..`) |
| `unresolved-entrypoint` | error | missing `__init__.py` / `register(ctx)`, empty SKILL.md body, transport without command/url, dashboard entry that doesn't exist |
| `unresolved-dependency` | warning | `requires_plugins` / `related_skills` that resolve to nothing source-owned (advisory, matching loader semantics) |
| `invalid-env-declaration` | error | `requires_env`-style entry that is neither a name nor a `{name, ...}` mapping |
| `unknown-capability` | warning | capability id not in `hermes_cli.plugin_capabilities.CAPABILITY_REGISTRY` |
| `unknown-platform` | error | platform token outside `KNOWN_PLATFORMS` |
| `kind-family-mismatch` | error | manifest kind contradicts the category's discovery routing |
| `manifest-version-newer` / `-invalid` / `-unsupported` | warning / error (mcp) | schema version handling per family |

**Fail-closed vs warn-and-continue.** The zero-findings gate applies to
*source-owned* packages at authoring/CI boundaries (`hermes packages
lint`, the inventory test, skill create/edit validation, the MCP catalog
reader). Runtime loaders keep the documented warn-and-continue posture
for user-installed packages — unknown manifest fields are ignored, a v1
manifest with only `name` still loads, and `hermes plugins doctor`
surfaces contract findings as warnings so legacy plugins keep passing.

## Authoring flow

1. **Plugin:** `plugins/<name>/` (or `plugins/<category>/<name>/`) with
   `plugin.yaml` + `__init__.py` — see [Build a Hermes Plugin](plugins/index.md).
   Validate with `hermes plugins doctor` (runtime registration + contract
   findings).
2. **Skill:** `skills/<category>/<name>/SKILL.md` (or `optional-skills/`
   for heavy/niche skills) — see [Creating Skills](creating-skills.md) and
   the authoring standards in `AGENTS.md`. Validate with the skill linter
   (`python -m tools.skill_linter <dir>`).
3. **MCP entry:** `optional-mcps/<name>/manifest.yaml` with pinned
   transport (`manifest_version: 1`).
4. Before sending a PR: `hermes packages lint` must be clean; the CI
   inventory test enforces the same thing.

### Extension examples

Declare rich env requirements (both shapes are valid; the rich shape
drives setup prompts):

```yaml
# plugin.yaml — simple
requires_env:
  - MY_API_KEY

# plugin.yaml — rich
requires_env:
  - name: MY_API_KEY
    prompt: "MyService API key"
    url: "https://myservice.example/keys"
    password: true
```

Declare skill prerequisites the setup flow reads:

```yaml
prerequisites:
  env_vars: [MY_API_KEY]
  commands: [jq]
required_environment_variables:
  - name: MY_API_KEY
    prompt: "MyService API key"
    optional: true
```

## Migration path

`hermes packages migrate` applies the documented **mechanical**
migrations (idempotent; `--check` reports without writing):

- declare the category-derived `kind:` where a nested plugin's manifest
  omits it (`kind-family-mismatch`);
- rename `metadata.hermes.upstream_skill` → `upstream`;
- drop dead single-line fields with no consumer (`title`, `authors`).

A field with no consumer but an established consumed equivalent is
reported for manual migration rather than guessed — e.g.
`required_commands:` (read by nothing; the same-named key in
`tools/skills_tool.py` is a constant `[]` in the response payload) belongs
under `prerequisites.commands:`, which the setup flow actually reads.

Everything else is reported as *needs manual migration* — e.g. folding a
dead `triggers:` list into the body's `## When to Use` section, or
crediting an `author`. The August 2026 standardization sweep migrated
every source-owned package; the inventory test keeps the tree at zero
findings from here.

Adding a **new consumed field**: land the consumer and add the field to
the census in `agent/package_contract.py`
(`KNOWN_PLUGIN_MANIFEST_FIELDS`, `KNOWN_SKILL_FRONTMATTER_FIELDS`,
`KNOWN_SKILL_HERMES_KEYS`, `KNOWN_MCP_MANIFEST_FIELDS`,
`KNOWN_DASHBOARD_MANIFEST_FIELDS`) in the same PR. Fields without a
consumer don't get reserved — that's the no-speculative-fields rule.

## Compatibility policy

The contract *inherits* the
[native plugin compatibility contract](plugins/index.md#native-plugin-compatibility-contract):
manifests are open to additions, documented surfaces evolve additively,
and deprecations need a replacement, a once-per-process warning, and a
two-minor-release window. Contract-specific commitments:

- Public identities (plugin keys, skill names, MCP entry names) are
  stable. A rename ships a compatibility alias.
- `manifest_version` absent means v1 and stays supported forever; newer
  versions than the running Hermes understands load with unknown fields
  ignored.
- Both `requires_env` shapes, top-level skill `tags:`/`category:`
  (mirrored into `metadata.hermes.*` by the loader), and
  `pip_dependencies` (the v1 spelling of `python_dependencies`) are
  permanent compatibility aliases.

## Consumers (canonical vs documented adapters)

| Boundary | Status |
| --- | --- |
| `hermes_cli/plugins.py` (PluginManager) | canonical — imports `KNOWN_PLUGIN_MANIFEST_FIELDS`, `SUPPORTED_MANIFEST_VERSION`, `VALID_PLUGIN_KINDS` from the contract |
| `agent/skill_utils.py` (`parse_frontmatter`, platform matching, 60-char budget) | canonical primitives the contract builds on |
| `tools/skills_tool.py`, `tools/skills_hub.py`, `agent/skill_commands.py` | delegate frontmatter parsing to `agent.skill_utils` |
| `hermes_cli/mcp_catalog.py` | documented adapter — the fail-closed install boundary keeps its own `_MANIFEST_VERSION` literal so the supply-chain reader carries no extra import chain; equality with the contract is enforced by test |
| `hermes_cli/plugin_dev.py` (`hermes plugins doctor`) | runs contract findings as warnings on top of runtime validation |
| `tools/skill_linter.py` | advisory linter over contract primitives (platform census, description budget) |
| `tools/skill_manager_tool.py` (`_validate_frontmatter`) | documented adapter — fail-closed create/edit boundary with verbatim YAML errors |
| `hermes_cli/plugins_cmd.py` (`_read_manifest_info`) | documented adapter — display-only fast path for `hermes plugins list` |
| `hermes_cli/agent_plugins.py` | documented adapter for the external Agent Plugins v1 spec (portable packages) |
| `hermes_cli/web_server.py` (`_discover_dashboard_plugins`) | runtime dashboard discovery; path-safety semantics mirrored by the contract's `unsafe-path` rule |
| Packaging | runtime-resolved (env overrides + source layout; see `setup.py`) — no build-time manifest enumeration to keep in sync |
