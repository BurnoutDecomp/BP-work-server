from __future__ import annotations

from importlib.resources import files

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException
from starlette.responses import Response
from starlette.types import Scope

from bp_work_server.services.page import dashboard_page

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


class StaticAssets(StaticFiles):
    """/static, with the shared UI library revalidated on every use.

    static/b5-ui is a git submodule on the dist branch of b5-decomp-webui: built ES
    modules whose imports name no version. Without Cache-Control a browser caches by
    heuristic, so after a deploy it could pair a fresh app.js?v= with library modules
    from before it. The ETag turns each revalidation into a 304. Other files keep their
    ?v= tags. Hidden files, such as the submodule's .git link, are never served.
    """

    async def get_response(self, path: str, scope: Scope) -> Response:
        # get_path() joins with the OS separator.
        parts = path.replace("\\", "/").split("/")
        if any(part.startswith(".") for part in parts):
            raise HTTPException(status_code=404)
        response = await super().get_response(path, scope)
        if parts[0] == "b5-ui":
            response.headers["Cache-Control"] = "no-cache"
        return response


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    html = static_dir().joinpath("index.html").read_text(encoding="utf-8")
    # The figures go out in the page itself, for whoever reads it without running app.js.
    html = await dashboard_page(request.app, html)
    if not is_preview_bot(request.headers.get("user-agent")):
        html = html.replace(THEME_COLOR_META, "")
    # The page differs by client: no cache may hand a bot's copy to a browser. Its figures
    # are live: a cached copy is checked again first.
    return HTMLResponse(html, headers={"Vary": "User-Agent", "Cache-Control": "no-cache"})


# The page sends the download sign's links in its HTML, where any crawler can follow them.
# Behind them are the 5 GB build and its update, and every start is counted on the sign.
ROBOTS_TXT = """\
# The builds are 5 GB downloads, and every start is counted on the dashboard's sign:
# they are for people, not crawlers.
User-agent: *
Disallow: /download/
"""


@router.get("/robots.txt", response_class=PlainTextResponse)
def robots() -> PlainTextResponse:
    return PlainTextResponse(ROBOTS_TXT)


@router.get("/health")
def health() -> dict[str, str]:
    from bp_work_server import __version__

    return {"ok": "true", "version": __version__}
