from __future__ import annotations

from importlib.resources import files

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

router = APIRouter()

# <meta name="theme-color"> is for link previews: Discord draws the embed's left bar in it.
# Browsers do not get it. On a hard reload Chrome painted every scrollbar of the page in that
# red, thumb and track alike, over the page's own scrollbar-color.
THEME_COLOR_META = '<meta name="theme-color" content="#e10600" />'
PREVIEW_BOTS = (
    "discordbot", "twitterbot", "facebookexternalhit", "slackbot", "telegrambot", "whatsapp",
    "linkedinbot", "embedly", "redditbot", "skypeuripreview", "vkshare", "mastodon", "bluesky",
)


def is_preview_bot(user_agent: str | None) -> bool:
    ua = (user_agent or "").lower()
    return any(bot in ua for bot in PREVIEW_BOTS)


def static_dir():
    return files("bp_work_server").joinpath("static")


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request) -> HTMLResponse:
    html = static_dir().joinpath("index.html").read_text(encoding="utf-8")
    if not is_preview_bot(request.headers.get("user-agent")):
        html = html.replace(THEME_COLOR_META, "")
    # the page differs by client: no cache may hand a bot's copy to a browser
    return HTMLResponse(html, headers={"Vary": "User-Agent"})


@router.get("/health")
def health() -> dict[str, str]:
    from bp_work_server import __version__

    return {"ok": "true", "version": __version__}
