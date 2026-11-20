from __future__ import annotations

from importlib.resources import files

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response
from starlette.types import Scope

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

    static/b5-ui is a build of b5-decomp-webui loaded as ES modules, and their imports
    name no version. Without Cache-Control a browser caches by heuristic, so after a
    deploy it could pair a fresh app.js?v= with library modules from before it. The
    ETag turns each revalidation into a 304. Other files keep their ?v= tags.
    """

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        # get_path() joins with the OS separator.
        if path.replace("\\", "/").startswith("b5-ui/"):
            response.headers["Cache-Control"] = "no-cache"
        return response


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
