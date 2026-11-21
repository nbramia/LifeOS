"""Registry of social platforms a contact can have accounts on.

Adding a platform is one ``SocialPlatform`` entry here: links are stored as
JSON rows keyed by the platform ``key``, so no schema change is involved, and
the API, CRM page, profile-URL matching, and export reader all read this list.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class ProfileRule:
    """How one URL shape on a platform identifies an account.

    ``pattern`` is matched against the URL path; group 1 is the account id
    unless ``query_param`` names the query parameter holding it instead.
    ``kind`` namespaces ids that are not interchangeable with handles.
    """

    pattern: str
    kind: str = ""
    query_param: str | None = None
    preserve_case: bool = False
    url_template: str | None = None


@dataclass(frozen=True)
class SocialPlatform:
    """One supported platform."""

    key: str
    label: str
    icon: str
    hosts: tuple[str, ...]
    rules: tuple[ProfileRule, ...]
    handle_pattern: str
    url_template: str | None  # None when a bare handle has no profile URL
    placeholder: str
    export_columns: tuple[str, ...]
    reserved_paths: frozenset[str] = frozenset()
    id_pattern: str | None = None  # bare values that are numeric account ids
    id_url_template: str | None = None


SOCIAL_PLATFORMS: tuple[SocialPlatform, ...] = (
    SocialPlatform(
        key="linkedin",
        label="LinkedIn",
        icon="💼",
        hosts=("linkedin.com",),
        rules=(
            ProfileRule(r"^/in/([^/]+)"),
            ProfileRule(
                r"^/pub/([^/]+(?:/[^/]+)*)",
                kind="pub",
                url_template="https://www.linkedin.com/pub/{id}",
            ),
            ProfileRule(
                r"^/profile/view/?$",
                kind="id",
                query_param="id",
                preserve_case=True,
                url_template="https://www.linkedin.com/profile/view?id={id}",
            ),
        ),
        handle_pattern=r"[^\s/?#@:]{1,200}",
        url_template="https://www.linkedin.com/in/{id}",
        placeholder="https://www.linkedin.com/in/username",
        export_columns=("linkedin", "linked in"),
    ),
    SocialPlatform(
        key="x",
        label="X (Twitter)",
        icon="𝕏",
        hosts=("x.com", "twitter.com"),
        rules=(ProfileRule(r"^/([A-Za-z0-9_]{1,15})(?:/|$)"),),
        handle_pattern=r"[A-Za-z0-9_]{1,15}",
        url_template="https://x.com/{id}",
        placeholder="@username",
        export_columns=("twitter x", "x twitter", "twitter", "x"),
        reserved_paths=frozenset({
            "home", "i", "intent", "search", "share", "hashtag",
            "explore", "settings", "messages", "notifications",
        }),
    ),
    SocialPlatform(
        key="facebook",
        label="Facebook",
        icon="📘",
        hosts=("facebook.com", "fb.com", "fb.me"),
        rules=(
            ProfileRule(r"^/profile\.php/?$", kind="id", query_param="id"),
            ProfileRule(r"^/people/[^/]+/(\d+)", kind="id"),
            ProfileRule(r"^/(\d{5,})(?:/|$)", kind="id"),
            ProfileRule(r"^/([\w.\-]+)(?:/|$)"),
        ),
        handle_pattern=r"[\w.\-]{1,80}",
        url_template="https://www.facebook.com/{id}",
        placeholder="https://www.facebook.com/username",
        export_columns=("facebook", "fb"),
        reserved_paths=frozenset({
            "groups", "pages", "events", "watch", "marketplace", "share",
            "sharer", "sharer.php", "login", "photo.php", "story.php",
            "permalink.php", "home.php", "l.php", "gaming", "hashtag",
        }),
        id_pattern=r"\d{5,}",
        id_url_template="https://www.facebook.com/profile.php?id={id}",
    ),
    SocialPlatform(
        key="instagram",
        label="Instagram",
        icon="📷",
        hosts=("instagram.com", "instagr.am"),
        rules=(ProfileRule(r"^/([A-Za-z0-9._]{1,30})(?:/|$)"),),
        handle_pattern=r"[A-Za-z0-9._]{1,30}",
        url_template="https://www.instagram.com/{id}",
        placeholder="@username",
        export_columns=("instagram", "insta", "ig"),
        reserved_paths=frozenset({"p", "reel", "reels", "explore", "stories", "tv", "accounts"}),
    ),
    SocialPlatform(
        key="telegram",
        label="Telegram",
        icon="✈️",
        hosts=("t.me", "telegram.me", "telegram.dog"),
        rules=(
            ProfileRule(r"^/s/([A-Za-z]\w{3,31})(?:/|$)"),
            ProfileRule(r"^/([A-Za-z]\w{3,31})(?:/|$)"),
        ),
        handle_pattern=r"[A-Za-z]\w{3,31}",
        url_template="https://t.me/{id}",
        placeholder="@username",
        export_columns=("telegram",),
        reserved_paths=frozenset({"joinchat", "addstickers", "addtheme", "share", "proxy", "iv"}),
    ),
    SocialPlatform(
        key="tiktok",
        label="TikTok",
        icon="🎵",
        hosts=("tiktok.com",),
        rules=(ProfileRule(r"^/@([A-Za-z0-9._]{1,24})(?:/|$)"),),
        handle_pattern=r"[A-Za-z0-9._]{1,24}",
        url_template="https://www.tiktok.com/@{id}",
        placeholder="@username",
        export_columns=("tiktok", "tik tok"),
    ),
    SocialPlatform(
        key="youtube",
        label="YouTube",
        icon="▶️",
        hosts=("youtube.com",),
        rules=(
            ProfileRule(r"^/@([^/]+)"),
            ProfileRule(
                r"^/channel/([^/]+)",
                kind="channel",
                preserve_case=True,
                url_template="https://www.youtube.com/channel/{id}",
            ),
            ProfileRule(r"^/c/([^/]+)", kind="c", url_template="https://www.youtube.com/c/{id}"),
            ProfileRule(r"^/user/([^/]+)", kind="user", url_template="https://www.youtube.com/user/{id}"),
        ),
        handle_pattern=r"[A-Za-z0-9._\-]{3,30}",
        url_template="https://www.youtube.com/@{id}",
        placeholder="@channel",
        export_columns=("youtube", "you tube"),
    ),
    SocialPlatform(
        key="discord",
        label="Discord",
        icon="🎮",
        hosts=("discord.com", "discordapp.com"),
        rules=(
            ProfileRule(
                r"^/users/(\d{17,20})(?:/|$)",
                kind="id",
                url_template="https://discord.com/users/{id}",
            ),
        ),
        handle_pattern=r"[A-Za-z0-9_.]{2,32}|[^#\s]{2,32}#\d{4}",
        url_template=None,
        placeholder="username",
        export_columns=("discord",),
        id_pattern=r"\d{17,20}",
        id_url_template="https://discord.com/users/{id}",
    ),
)

PLATFORMS_BY_KEY: dict[str, SocialPlatform] = {p.key: p for p in SOCIAL_PLATFORMS}
