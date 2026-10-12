"""Shared declarations for package.json DSH configuration fields.

Port of ``@deepseek-ai/dsh-package-manifest`` (commit 5badb15). Upstream is a
types-only package (``export {}`` runtime): ``DshPackageManifest`` describes
the package.json fields DSH readers use — ``name``/``version`` required,
``engines.dsh`` a declared SemVer compatibility range, ``dsh.manifestVersion``
the declared format version (1), ``dsh.bundle.patch`` one ordered patch path
or list of paths relative to the package root, ``dsh.client`` the client
domain, plus localized display metadata with a required English fallback.
Readers own JSON parsing/validation; the package infers no defaults and
compatibility remains declarative (the installer does not enforce it).

Python adaptation: the declarations become frozen dataclasses plus ONE
reference reader (``parse_dsh_package_manifest``) implementing exactly the
documented field rules — fail-loud on malformed input, no inferred defaults,
``partial=True`` for profile readers that need no published version.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Union

MANIFEST_VERSION = 1


@dataclass(frozen=True)
class DshBundleManifest:
    """Ordered patch layer(s), each path relative to the package root."""
    patch: Union[str, tuple]

    def patch_paths(self) -> List[str]:
        return [self.patch] if isinstance(self.patch, str) else list(self.patch)


@dataclass(frozen=True)
class DshClientManifest:
    """Client domain declaration (e.g. platform 'web')."""
    platform: str


@dataclass(frozen=True)
class DshManifest:
    """Public author fields under the ``dsh`` key."""
    manifest_version: Optional[int] = None
    bundle: Optional[DshBundleManifest] = None
    client: Optional[DshClientManifest] = None
    title: Optional[Union[str, Dict[str, str]]] = None
    description: Optional[Union[str, Dict[str, str]]] = None


@dataclass(frozen=True)
class DshEngines:
    node: Optional[str] = None
    npm: Optional[str] = None
    dsh: Optional[str] = None


@dataclass(frozen=True)
class DshPackageManifest:
    """The package.json fields DSH readers consume."""
    name: Optional[str] = None
    version: Optional[str] = None
    icon: Optional[str] = None
    engines: Optional[DshEngines] = None
    dsh: Optional[DshManifest] = None
    extra: Dict[str, Any] = field(default_factory=dict)


def _localized_text(value: Any, path: str) -> Union[str, Dict[str, str]]:
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and value and all(
            isinstance(key, str) and isinstance(item, str) for key, item in value.items()):
        if "en" not in value:
            raise ValueError(f"{path}: localized text requires the English fallback 'en'")
        return dict(value)
    raise ValueError(f"{path}: must be literal text or a language map with an 'en' fallback")


def parse_dsh_package_manifest(data: Any, *, partial: bool = False) -> DshPackageManifest:
    """Reference reader for the declared fields; malformed input fails loud.

    ``partial=True`` mirrors profile readers (``Partial<DshPackageManifest>``):
    name/version optional because profiles need no published version.
    """
    if not isinstance(data, dict):
        raise ValueError("package manifest must be an object")
    name = data.get("name")
    version = data.get("version")
    if not partial:
        if not isinstance(name, str) or not name.strip():
            raise ValueError("name is required and must be a non-empty string")
        if not isinstance(version, str) or not version.strip():
            raise ValueError("version is required and must be a non-empty string")
    elif name is not None and not isinstance(name, str):
        raise ValueError("name must be a string when present")
    elif version is not None and not isinstance(version, str):
        raise ValueError("version must be a string when present")

    engines: Optional[DshEngines] = None
    if "engines" in data and data["engines"] is not None:
        raw = data["engines"]
        if not isinstance(raw, dict) or any(
                not isinstance(value, str) for value in raw.values()):
            raise ValueError("engines must be an object of string ranges")
        unknown = set(raw) - {"node", "npm", "dsh"}
        if unknown:
            raise ValueError(f"engines has unknown fields: {sorted(unknown)}")
        engines = DshEngines(node=raw.get("node"), npm=raw.get("npm"), dsh=raw.get("dsh"))

    icon = data.get("icon")
    if icon is not None and not isinstance(icon, str):
        raise ValueError("icon must be a string (data URL resolved by readers)")

    dsh: Optional[DshManifest] = None
    if "dsh" in data and data["dsh"] is not None:
        raw = data["dsh"]
        if not isinstance(raw, dict):
            raise ValueError("dsh must be an object")
        manifest_version = raw.get("manifestVersion")
        if manifest_version is not None and manifest_version != MANIFEST_VERSION:
            raise ValueError(f"dsh.manifestVersion: the declared format is {MANIFEST_VERSION}")
        bundle: Optional[DshBundleManifest] = None
        if "bundle" in raw and raw["bundle"] is not None:
            bundle_raw = raw["bundle"]
            if not isinstance(bundle_raw, dict) or "patch" not in bundle_raw:
                raise ValueError("dsh.bundle must declare patch")
            patch = bundle_raw["patch"]
            if isinstance(patch, str):
                if not patch.strip():
                    raise ValueError("dsh.bundle.patch must be a non-empty path")
                bundle = DshBundleManifest(patch=patch)
            elif isinstance(patch, list) and patch and all(
                    isinstance(item, str) and item.strip() for item in patch):
                bundle = DshBundleManifest(patch=tuple(patch))
            else:
                raise ValueError("dsh.bundle.patch must be a path or an ordered list of paths")
            unknown = set(bundle_raw) - {"patch"}
            if unknown:
                raise ValueError(f"dsh.bundle has unknown fields: {sorted(unknown)}")
        client: Optional[DshClientManifest] = None
        if "client" in raw and raw["client"] is not None:
            client_raw = raw["client"]
            if not isinstance(client_raw, dict) or not isinstance(client_raw.get("platform"), str):
                raise ValueError("dsh.client must declare a string platform")
            client = DshClientManifest(platform=client_raw["platform"])
        title = _localized_text(raw["title"], "dsh.title") if "title" in raw else None
        description = _localized_text(raw["description"], "dsh.description") if "description" in raw else None
        dsh = DshManifest(manifest_version=manifest_version, bundle=bundle, client=client,
                          title=title, description=description)

    known = {"name", "version", "icon", "engines", "dsh"}
    return DshPackageManifest(name=name, version=version, icon=icon, engines=engines, dsh=dsh,
                              extra={key: value for key, value in data.items() if key not in known})
