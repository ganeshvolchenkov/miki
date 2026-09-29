"""What is banned during a focus session. Pure functions, no I/O, so every rule is easy to test.

Everything is allowed except a list of distractions: sites (YouTube, Netflix, Reddit ...) and programs
(Discord, Steam ...). The list ships with sensible defaults and you can add to it or take things off it.

Sites match on whole domain labels (``notyoutube.com`` is not ``youtube.com``), and only the real host of a URL
counts (``https://youtube.com@evil.com`` is evil.com). A browser we can't see into (your everyday Chrome, Edge,
Firefox) is judged by its window title, which always carries the name of the page in front.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

DEFAULT_BANNED_SITES: tuple[str, ...] = (
    "youtube.com", "youtu.be", "tiktok.com", "instagram.com", "reddit.com", "netflix.com", "twitch.tv", "kick.com",
    "facebook.com", "twitter.com", "x.com", "9gag.com", "pinterest.com", "snapchat.com",
    "disneyplus.com", "primevideo.com", "hulu.com", "discord.com", "web.whatsapp.com", "store.steampowered.com",
)

DEFAULT_BANNED_APPS: tuple[str, ...] = (
    "discord.exe", "steam.exe", "steamwebhelper.exe", "epicgameslauncher.exe", "battle.net.exe", "leagueclient.exe",
    "riotclientservices.exe", "robloxplayerbeta.exe", "whatsapp.exe", "netflix.exe", "tiktok.exe",
)

# Programs that show web pages. When one of these is in front and it isn't the Miki-watched Chrome, its window
# title (the page in front) is checked against the banned names.
BROWSERS: frozenset[str] = frozenset({"chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe", "vivaldi.exe", "arc.exe"})

_DOMAIN = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9-]{2,63}$")
_APP = re.compile(r"^[a-z0-9][a-z0-9 ._+-]{0,60}$")
_MIN_KEYWORD = 5  # "x.com" -> "x" would match half the web, so short names are only matched as URLs


@dataclass(frozen=True)
class Target:
    kind: str  # "site" or "app"
    value: str  # "youtube.com" or "discord.exe"


def normalize_site(text: object) -> str | None:
    """'https://Www.YouTube.com/x' -> 'youtube.com'. None if it isn't a usable domain."""
    raw = str(text or "").strip().lower()
    if not raw:
        return None
    if "://" in raw:
        raw = urlsplit(raw).hostname or ""
    raw = raw.split("/")[0].strip().rstrip(".")
    if raw.startswith("www."):
        raw = raw[4:]
    return raw if _DOMAIN.match(raw) else None


def parse_target(text: object) -> Target | None:
    """What did the user mean by ``discord``, ``discord.exe``, ``reddit.com`` or a full URL?

    A name with a dot (and not ending in .exe) is a website; anything else is a program.
    """
    raw = str(text or "").strip().lower()
    if not raw:
        return None
    if "://" in raw or ("." in raw and not raw.endswith(".exe")):
        site = normalize_site(raw)
        return Target("site", site) if site else None
    name = raw if raw.endswith(".exe") else raw + ".exe"
    return Target("app", name) if _APP.match(name[:-4]) else None


def parse_targets(raw: str | None) -> tuple[list[str], list[str]]:
    """A comma/space separated setting -> (sites, apps), clean and de-duplicated. Invalid entries are dropped."""
    sites: dict[str, None] = {}
    apps: dict[str, None] = {}
    for part in re.split(r"[,;\s]+", raw or ""):
        target = parse_target(part)
        if target:
            (sites if target.kind == "site" else apps)[target.value] = None
    return list(sites), list(apps)


def site_banned(host: str, banned: tuple[str, ...] | list[str]) -> bool:
    host = (host or "").strip().lower().rstrip(".")
    return bool(host) and any(host == entry or host.endswith("." + entry) for entry in banned)


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    label: str  # what to call it when it is blocked: the domain


def judge_url(url: str, banned: tuple[str, ...] | list[str]) -> Verdict:
    """May a browser tab stay on ``url``? Only websites on the banned list are turned away."""
    try:
        parts = urlsplit((url or "").strip())
    except ValueError:
        return Verdict(True, "")
    if parts.scheme.lower() not in {"http", "https"}:
        return Verdict(True, "")  # blank pages, chrome:// pages, local files
    host = (parts.hostname or "").lower().rstrip(".")
    return Verdict(not site_banned(host, banned), host)


def keywords_for(sites: tuple[str, ...] | list[str]) -> dict[str, str]:
    """Words to look for in a window title -> the site they stand for. 'youtube.com' -> {'youtube': 'youtube.com'}."""
    words: dict[str, str] = {}
    for site in sites:
        labels = site.split(".")
        name = labels[-2] if len(labels) >= 2 else labels[0]
        if len(name) >= _MIN_KEYWORD:
            words.setdefault(name, site)
    return words


def title_hit(title: str, sites: tuple[str, ...] | list[str]) -> str | None:
    """The banned site named in a browser window's title, or None. ('Cats - YouTube - Google Chrome' -> 'youtube.com')"""
    text = (title or "").lower()
    for word, site in keywords_for(sites).items():
        if re.search(rf"(?<![a-z0-9]){re.escape(word)}(?![a-z0-9])", text):
            return site
    return None


def app_banned(exe: str, banned: frozenset[str] | set[str] | tuple[str, ...]) -> bool:
    return (exe or "").strip().lower() in banned


def describe_banned(sites: tuple[str, ...] | list[str], apps: tuple[str, ...] | list[str]) -> str:
    """A short human list for messages: 'YouTube, Netflix, Reddit, Discord and 20 more'."""
    pretty = {"youtube.com": "YouTube", "x.com": "X", "youtu.be": None, "web.whatsapp.com": "WhatsApp Web", "store.steampowered.com": "Steam store",
              "twitter.com": "Twitter", "9gag.com": "9GAG", "tiktok.com": "TikTok", "primevideo.com": "Prime Video",
              "disneyplus.com": "Disney+", "discord.com": "Discord", "steam.exe": "Steam", "discord.exe": "Discord"}
    names: dict[str, None] = {}
    for entry in [*sites, *apps]:
        name = pretty[entry] if entry in pretty else entry.removesuffix(".exe").split(".")[0].capitalize()
        if name:
            names[name] = None
    shown = list(names)[:6]
    extra = len(names) - len(shown)
    return ", ".join(shown) + (f" and {extra} more" if extra > 0 else "")
