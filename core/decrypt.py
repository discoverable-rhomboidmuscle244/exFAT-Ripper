"""
core/decrypt.py
---------------
Offline Link Lock decryption. The .exFAT site wraps every download link in a
jstrieb/link-lock URL — AES-GCM ciphertext, key derived via PBKDF2-HMAC-SHA256
(100,000 iterations). The locked URL is captured during the fast page-walk;
this module turns it into the real download URL with no browser, instantly.
"""
from __future__ import annotations

import base64
import json

from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

PBKDF2_ITERATIONS = 100_000

# Cache so repeated identical locked URLs only decrypt once per session.
_cache: dict[tuple[str, str], str] = {}


def is_link_lock_url(url: str) -> bool:
    u = url or ""
    return "link-lock" in u and "#" in u


def decrypt_link_lock(url: str, password: str) -> str:
    """Decrypt a link-lock URL -> the original destination URL.
    Returns '' on any failure (wrong password, malformed fragment)."""
    if not is_link_lock_url(url):
        return ""
    ckey = (url, password)
    if ckey in _cache:
        return _cache[ckey]
    try:
        frag = url.split("#", 1)[1].strip()
        frag += "=" * (-len(frag) % 4)  # restore base64 padding
        payload = json.loads(base64.b64decode(frag))
        e = base64.b64decode(payload["e"])   # ciphertext + GCM tag
        s = base64.b64decode(payload["s"])   # salt
        i = base64.b64decode(payload["i"])   # iv / nonce
        kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=s,
                         iterations=PBKDF2_ITERATIONS)
        key = kdf.derive(password.encode("utf-8"))
        plaintext = AESGCM(key).decrypt(i, e, None)
        result = plaintext.decode("utf-8", errors="replace").strip()
    except Exception:
        result = ""
    _cache[ckey] = result
    return result


# Host classification from a resolved URL.
_HOST_KEYS = [
    ("akirabox.",   "akirabox"),
    ("datanodes.",  "datanodes"),
    ("vikingfile.", "vikingfile"),
    ("vik1ngfile.", "vikingfile"),
    ("buzzheavier", "buzzheavier"),
    ("mediafire.",  "mediafire"),
    ("1fichier.",   "1fichier"),
    ("pixeldrain.", "pixeldrain"),
    ("gofile.",     "gofile"),
    ("mega.",       "mega"),
    ("rapidgator.", "rapidgator"),
    ("rootz.",      "rootz"),
]


def host_for(url: str) -> str:
    u = (url or "").lower()
    for needle, key in _HOST_KEYS:
        if needle in u:
            return key
    return "other"


def resolve_game_links(game: dict, password: str) -> dict:
    """Given a game dict with `exfat_raw_sections` holding locked URLs,
    decrypt every link and return a structure ready for the UI:

        {
          "sections": {
             "GAME":     [{"host","url","is_dlc","is_dump"}, ...],
             "BACKPORT": [...],
             "DLC":      [...],
             ...
          }
        }
    """
    raw = game.get("exfat_raw_sections") or {}
    out: dict[str, list[dict]] = {}

    def label_of(sec_name: str) -> str:
        n = (sec_name or "").lower()
        if n in ("game", "standard", ""):
            return "GAME"
        if n.startswith("backport") or n.startswith("bp_") or n.startswith("bp-"):
            return "BACKPORT"
        if "dlc" in n:
            return "DLC"
        if "dump" in n:
            return "DUMP"
        if "fix" in n or "patch" in n:
            return "FIX"
        if "update" in n:
            return "UPDATE"
        return "OTHER"

    for sec_name, recs in raw.items():
        if not isinstance(recs, list):
            continue
        for r in recs:
            base = label_of(sec_name)
            if r.get("is_dlc"):
                base = "DLC"
            if r.get("is_dump"):
                base = "DUMP"
            locked = r.get("locked_url", "")
            # Prefer a fresh offline decrypt; fall back to any stored resolved.
            url = decrypt_link_lock(locked, password) or r.get("resolved_url", "")
            if not url:
                continue
            out.setdefault(base, []).append({
                "host": host_for(url),
                "url": url,
                "label": r.get("label", ""),
                "is_dlc": bool(r.get("is_dlc")),
                "is_dump": bool(r.get("is_dump")),
            })

    # Convert the {LABEL: [links]} map into the {regions, builds} structure
    # the UI's detail panel now expects (same shape dlps_sections returns),
    # so exFAT and dlpsgame both feed renderLinks an identical structure.
    return _to_regions(game, out)


# Section ordering for a tidy detail panel.
_SECTION_ORDER = ["GAME", "BACKPORT", "DLC", "DUMP", "UPDATE", "FIX", "OTHER"]


def _to_regions(game: dict, sections: dict) -> dict:
    """Wrap an exFAT {LABEL: [links]} map as the regions/builds structure.

    exFAT games have no per-region packages, so the result is a single
    region whose builds are the GAME / BACKPORT / DLC / ... sections, each
    with its links grouped by host.
    """
    def build_title(label: str) -> str:
        v = (game.get("version") or "").strip()
        if label == "GAME":
            return f"Game ({v})" if v else "Game"
        if label == "BACKPORT":
            bp = game.get("backport") or {}
            fw = (bp.get("firmware") or "").strip()
            grp = (bp.get("release_group") or "").strip()
            t = "Backport"
            if fw:
                t += f" {fw}"
            if grp:
                t += f" (@{grp.lstrip('@')})"
            return t
        return label.title()

    builds = []
    ordered = sorted(
        sections.keys(),
        key=lambda k: _SECTION_ORDER.index(k) if k in _SECTION_ORDER else 99)
    for label in ordered:
        links = sections[label]
        if not links:
            continue
        # group this section's links by host
        by_host: dict[str, list[dict]] = {}
        for ln in links:
            by_host.setdefault(ln["host"], []).append(ln)
        hosts = []
        for host, items in by_host.items():
            hosts.append({
                "host": host,
                "links": [{"url": it["url"], "part": i + 1}
                          for i, it in enumerate(items)],
            })
        builds.append({
            "kind": label,
            "title": build_title(label),
            "version": game.get("version", "") or "",
            "firmware": "",
            "release_group": "",
            "hosts": hosts,
        })

    if not builds:
        return {"regions": [], "multi_region": False}

    return {
        "regions": [{
            "ppsa": (game.get("ppsa") or "").strip(),
            "region": (game.get("region") or "").strip(),
            "label": (game.get("ppsa") or "Download").strip(),
            "info": {
                "size": game.get("size", "") or "",
                "release_group": (game.get("credits") or {}).get("files", "")
                if isinstance(game.get("credits"), dict) else "",
            },
            "builds": builds,
        }],
        "multi_region": False,
    }
