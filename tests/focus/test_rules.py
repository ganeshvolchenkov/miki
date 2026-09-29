import pytest

from app.focus import rules

BANNED = rules.DEFAULT_BANNED_SITES


@pytest.mark.parametrize("url", [
    "https://www.youtube.com/watch?v=x",
    "https://youtube.com/",
    "https://m.youtube.com/shorts/abc",
    "https://youtu.be/abc",
    "https://www.reddit.com/r/all",
    "https://old.reddit.com/",
    "https://x.com/someone",
    "https://www.instagram.com/",
    "https://www.netflix.com/browse",
    "https://YOUTUBE.com./x",  # case and trailing dot
])
def test_distraction_sites_are_banned(url):
    assert not rules.judge_url(url, BANNED).allowed, url


@pytest.mark.parametrize("url", [
    "https://gemini.google.com/app",
    "https://claude.ai/new",
    "https://mail.google.com/mail/u/0/#inbox",
    "https://canvas.uva.nl/courses/1",
    "https://en.wikipedia.org/wiki/Eigenvalue",
    "https://stackoverflow.com/questions/1",
    "https://www.google.com/search?q=youtube.com",  # the banned name only in the query
    "https://notyoutube.com/",  # a different domain that merely ends the same
    "https://youtube.com.evil.com/",  # the real host is evil.com
    "https://evil.com/youtube.com",
    "https://box.com/",  # short names must not match inside other domains (x.com)
    "about:blank",
    "chrome://newtab/",
    "file:///C:/Users/me/Documents/lecture.pdf",
    "file:///C:/Users/me/Videos/movie.mp4",  # local files are your business
    "",
    "https://",
])
def test_everything_else_is_allowed(url):
    assert rules.judge_url(url, BANNED).allowed, url


def test_userinfo_trick_uses_the_real_host():
    assert not rules.judge_url("https://claude.ai@youtube.com/", BANNED).allowed
    assert rules.judge_url("https://youtube.com@claude.ai/", BANNED).allowed


def test_the_verdict_names_the_site():
    assert rules.judge_url("https://www.youtube.com/watch?v=x", BANNED).label == "www.youtube.com"


def test_your_own_bans_work_with_subdomains():
    banned = (*BANNED, "9anime.to")
    assert not rules.judge_url("https://www.9anime.to/x", banned).allowed
    assert rules.judge_url("https://9anime.top/", banned).allowed


@pytest.mark.parametrize("raw, expected", [
    ("https://Www.YouTube.com/x", ("site", "youtube.com")),
    ("tiktok.com", ("site", "tiktok.com")),
    ("  Example.ORG/path ", ("site", "example.org")),
    ("discord", ("app", "discord.exe")),
    ("Discord.EXE", ("app", "discord.exe")),
    ("battle.net.exe", ("app", "battle.net.exe")),
    ("league of legends", ("app", "league of legends.exe")),
    ("", None),
    (None, None),
    ("javascript:alert(1)", None),
    ("http://", None),
    ("a/b\\c", None),
])
def test_parse_target(raw, expected):
    target = rules.parse_target(raw)
    assert (None if target is None else (target.kind, target.value)) == expected


def test_parse_targets_splits_sites_and_apps():
    sites, apps = rules.parse_targets("tiktok.com, discord  https://twitch.tv/x;steam.exe junk!!")
    assert sites == ["tiktok.com", "twitch.tv"] and apps == ["discord.exe", "steam.exe"]
    assert rules.parse_targets(None) == ([], [])


def test_title_matching_for_browsers_we_cannot_see_into():
    assert rules.title_hit("Cute cats - YouTube - Google Chrome", BANNED) == "youtube.com"
    assert rules.title_hit("(3) Instagram - Personal - Microsoft Edge", BANNED) == "instagram.com"
    assert rules.title_hit("r/all - Reddit", BANNED) == "reddit.com"
    assert rules.title_hit("Lecture5_Eigenvalues.pdf - Google Chrome", BANNED) is None
    assert rules.title_hit("Claude - Google Chrome", BANNED) is None
    assert rules.title_hit("Why I stopped using YouTubers", BANNED) is None  # whole words only
    assert rules.title_hit("", BANNED) is None


def test_short_site_names_are_only_matched_as_urls():
    # "x.com" would otherwise match any title containing an x
    assert rules.title_hit("Fox news - Google Chrome", ("x.com",)) is None
    assert not rules.judge_url("https://x.com/a", ("x.com",)).allowed


def test_apps():
    banned = frozenset(rules.DEFAULT_BANNED_APPS)
    assert rules.app_banned("Discord.EXE", banned)
    assert rules.app_banned("steam.exe", banned)
    assert not rules.app_banned("code.exe", banned)
    assert not rules.app_banned("explorer.exe", banned)
    assert not rules.app_banned("", banned)


def test_description_is_friendly():
    text = rules.describe_banned(rules.DEFAULT_BANNED_SITES, rules.DEFAULT_BANNED_APPS)
    assert text.startswith("YouTube, TikTok, Instagram, Reddit") and "more" in text
    assert "youtu.be" not in text and "Youtu" not in text
