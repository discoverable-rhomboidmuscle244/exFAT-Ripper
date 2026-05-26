"""
core/dlps_links.py
------------------
Turns a dlpsgame game dict into the structure the UI's detail panel needs.

dlpsgame links are already plain host URLs (no decryption). The challenge is
shape: a post can have several regions (PPSA packages), each region has
several builds (Game / Backport / DLC), and each build has links across
several hosts — occasionally 20+ parts on one host.

This module returns, per game:

  {
    "regions": [
      {
        "ppsa": "PPSA03659",
        "region": "EUR",
        "label": "PPSA03659 – EUR",
        "info": { "password": "...", "note": "...", "uploader": "...",
                  "fw_required": "...", "size": "...",
                  "screen_languages": "...", "voice_languages": "..." },
        "builds": [
          {
            "kind": "GAME",                  # GAME / BACKPORT / DLC / ...
            "title": "Game (v01.006)",       # human label for the build line
            "version": "v01.006",
            "firmware": "",                  # backport firmware if any
            "release_group": "",
            "hosts": [
              { "host": "mediafire",
                "links": [ {url, part}, ... ] },   # 1 entry per host
              ...
            ]
          },
          ...
        ]
      },
      ...
    ],
    "multi_region": true/false
  }

The UI renders one line per build, host names inline as links. A host that
has many parts is shown inline as an expandable "host (N parts)" chip, so a
20-part release never blows up the line.
"""
from __future__ import annotations

from urllib.parse import urlparse


_HOST_KEYS = [
    ("mediafire.",   "mediafire"),
    ("1fichier.",    "1fichier"),
    ("pixeldrain.",  "pixeldrain"),
    ("gofile.",      "gofile"),
    ("akirabox.",    "akirabox"),
    ("datanodes.",   "datanodes"),
    ("vikingfile.",  "vikingfile"),
    ("vik1ngfile.",  "vikingfile"),
    ("buzzheavier",  "buzzheavier"),
    ("mega.",        "mega"),
    ("rapidgator.",  "rapidgator"),
    ("nitroflare.",  "nitroflare"),
    ("turbobit.",    "turbobit"),
    ("krakenfiles.", "krakenfiles"),
    ("rootz.",       "rootz"),
    ("uploadhaven.", "uploadhaven"),
    ("filecrypt.",   "filecrypt"),
]


def host_for(url: str) -> str:
    u = (url or "").lower()
    for needle, key in _HOST_KEYS:
        if needle in u:
            return key
    try:
        net = urlparse(url).netloc.lower()
        if net:
            parts = net.split(".")
            return parts[-2] if len(parts) >= 2 else net
    except Exception:
        pass
    return "other"


_KIND_LABEL = {
    "game":     "GAME",
    "standard": "GAME",
    "backport": "BACKPORT",
    "dlc":      "DLC",
    "dlcs":     "DLC",
    "update":   "UPDATE",
    "fix":      "FIX",
    "patch":    "FIX",
    "other":    "OTHER",
}


def _kind_label(kind: str) -> str:
    return _KIND_LABEL.get((kind or "").lower(), "OTHER")


def _build_title(kind_label: str, entry: dict) -> str:
    """A human label for a build line, e.g. 'Game (v01.006)' or
    'Backport 4.xx (@BADERLINK)'."""
    ver = (entry.get("version") or "").strip()
    fw = (entry.get("firmware") or "").strip()
    grp = (entry.get("release_group") or "").strip()
    if kind_label == "BACKPORT":
        bits = "Backport"
        if fw:
            bits += f" {fw}"
        if grp:
            bits += f" (@{grp.lstrip('@')})"
        return bits
    name = kind_label.title()           # Game / Dlc / Update / Fix
    if ver:
        name += f" ({ver})"
    return name


def _hosts_from_links(links: dict) -> list:
    """links is {host: [urls]} -> [ {host, links:[{url,part}]} ]."""
    out = []
    for host_raw, urls in (links or {}).items():
        items = []
        for i, u in enumerate(urls or [], 1):
            if u:
                items.append({"url": u, "part": i})
        if items:
            out.append({"host": host_for(items[0]["url"]) or host_raw,
                         "links": items})
    return out


def _builds_from_releases(releases: dict) -> list:
    """A releases dict {kind: [entry,...]} -> a flat list of build dicts."""
    builds = []
    # stable order: game, backport, dlc, update, fix, other
    order = ["game", "standard", "backport", "dlc", "dlcs",
             "update", "fix", "patch", "other"]
    keys = sorted(releases.keys(),
                  key=lambda k: order.index(k.lower())
                  if k.lower() in order else 99)
    for kind in keys:
        kl = _kind_label(kind)
        for entry in releases.get(kind) or []:
            hosts = _hosts_from_links(entry.get("links") or {})
            if not hosts:
                continue
            builds.append({
                "kind": kl,
                "title": _build_title(kl, entry),
                "version": entry.get("version", "") or "",
                "firmware": entry.get("firmware", "") or "",
                "release_group": entry.get("release_group", "") or "",
                "hosts": hosts,
            })
    return builds


def _pkg_info(pkg: dict) -> dict:
    """Extract the human-readable extras for a package/region."""
    return {
        "password":        pkg.get("password", "") or "",
        "note":            pkg.get("note", "") or "",
        "uploader":        pkg.get("uploader", "") or "",
        "fw_required":     pkg.get("fw_required", "") or "",
        "size":            pkg.get("size", "") or "",
        "screen_languages": pkg.get("screen_languages", "") or "",
        "voice_languages":  pkg.get("voice_languages", "") or "",
        "release_group":   pkg.get("release_group", "") or "",
    }


def dlps_sections(game: dict) -> dict:
    """Build the regions/builds structure described in the module docstring."""
    regions = []

    packages = game.get("packages")
    if isinstance(packages, list) and packages:
        for pkg in packages:
            rels = pkg.get("releases")
            builds = _builds_from_releases(rels) if isinstance(rels, dict) else []
            if not builds:
                continue
            ppsa = (pkg.get("ppsa") or "").strip()
            region = (pkg.get("region") or "").strip()
            label = (pkg.get("label") or "").strip() or \
                    " – ".join(x for x in (ppsa, region) if x) or "PACKAGE"
            regions.append({
                "ppsa": ppsa,
                "region": region,
                "label": label,
                "info": _pkg_info(pkg),
                "builds": builds,
            })

    # Fallback for older data with no packages: use the flattened releases.
    if not regions:
        rels = game.get("releases")
        builds = _builds_from_releases(rels) if isinstance(rels, dict) else []
        if not builds:
            # last resort: flat links map
            flat = game.get("links")
            if isinstance(flat, dict) and flat:
                clean = {h: u for h, u in flat.items()
                         if not h.startswith("_")}
                hosts = _hosts_from_links(clean)
                if hosts:
                    builds = [{
                        "kind": "GAME", "title": "Game",
                        "version": game.get("version", "") or "",
                        "firmware": "", "release_group": "",
                        "hosts": hosts,
                    }]
        if builds:
            regions.append({
                "ppsa": (game.get("ppsa") or "").strip(),
                "region": (game.get("region") or "").strip(),
                "label": (game.get("ppsa") or "Download").strip(),
                "info": _pkg_info(game),
                "builds": builds,
            })

    return {
        "regions": regions,
        "multi_region": len(regions) > 1,
    }
