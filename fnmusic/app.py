import asyncio
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Request
from starlette.responses import FileResponse, JSONResponse

from .audio import Audio
from .config import Config
from .jobs import Jobs
from .lx import LX
from .media import Media
from .models import to_official
from .official import Official, session_key
from .playlists import DISCOVERY_KINDS, Playlists
from .store import Store
from .transcode import Transcode
from .logs import event, activity, safe_route
from .images import image_response, playlist_cover_path

PREFIX = "/music/api/v1/"


def envelope(data):
    return {"code": 0, "msg": "", "data": data}


def create_app(
    config=None, official_client=None, lx_client=None, media_client=None, *, worker=True
):
    config = config or Config.from_env()
    session_users = {}
    native_items = {}

    @asynccontextmanager
    async def lifespan(app):
        config.data_dir.mkdir(parents=True, exist_ok=True)
        store = Store(config.data_dir / "state.db")
        oc = official_client or httpx.AsyncClient(
            base_url="http://official",
            transport=httpx.AsyncHTTPTransport(uds=config.upstream_socket),
            timeout=20,
            trust_env=False,
        )
        lc = lx_client or httpx.AsyncClient(
            base_url=config.lx_url, timeout=20, trust_env=False
        )
        mc = media_client or httpx.AsyncClient(
            timeout=httpx.Timeout(30, read=45), trust_env=False
        )
        official = Official(oc, config)
        lx = LX(lc, config.lx_password, config.platforms)
        app.state.store = store
        app.state.official = official
        app.state.lx = lx
        app.state.media = Media(mc, lx, official, store, config)
        app.state.transcode = Transcode(app.state.media, config)
        app.state.playlists = Playlists(store, lx, config)
        jobs = Jobs(store, lx, Audio(mc, lx, config), official, config)
        app.state.jobs = jobs
        if worker:
            jobs.start()
            app.state.playlists.start(jobs, official)
        event(config.data_dir, "proxy", "service.started")
        try:
            yield
        finally:
            await app.state.playlists.stop()
            await jobs.stop()
            await app.state.transcode.stop()
            await oc.aclose()
            await lc.aclose()
            await mc.aclose()
            store.close()
            event(config.data_dir, "proxy", "service.stopped")

    app = FastAPI(
        title="fnMusic LX 扩展",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    search_cache = {}

    @app.exception_handler(ValueError)
    async def value_error(request, exc):
        event(
            config.data_dir,
            "proxy",
            "response.invalid",
            level="ERROR",
            error=exc,
            route=safe_route(request.url.path),
            method=request.method,
        )
        return JSONResponse(
            {"code": -1, "msg": "音源或官方接口响应异常，请检查配置", "data": None},
            status_code=502,
        )

    @app.exception_handler(httpx.HTTPError)
    async def upstream_error(request, exc):
        event(
            config.data_dir,
            "proxy",
            "upstream.failed",
            level="ERROR",
            error=exc,
            route=safe_route(request.url.path),
            method=request.method,
        )
        return JSONResponse(
            {"code": -1, "msg": "上游服务不可用", "data": None}, status_code=502
        )

    async def identity(request):
        user = await app.state.official.user(request.headers)
        request.state.music_user = user
        if len(session_users) >= 500:
            session_users.pop(next(iter(session_users)))
        session_users[session_key(request.headers)] = (time.monotonic() + 300, user)
        app.state.jobs.touch(user, request.headers)
        return user

    def known_user(request):
        cached = session_users.get(session_key(request.headers))
        return cached[1] if cached and cached[0] > time.monotonic() else None

    async def playlist_rows(request, user, guid):
        rows = await app.state.playlists.tracks(
            user, guid, request.headers, app.state.official,
        )
        if app.state.playlists.key(user, guid) == "archaeology":
            if len(native_items) > 3000:
                native_items.clear()
            for row in rows:
                native_items[(user, row["guid"])] = (time.monotonic() + 900, row)
        return rows

    async def discovery_card_counts(request, user, cards):
        playlists = app.state.playlists
        discovery_guids = {playlists.guid(user, kind) for kind in DISCOVERY_KINDS}

        async def with_count(card):
            guid = card["guid"]
            if guid not in discovery_guids:
                return card
            try:
                rows = await playlists.tracks(
                    user, guid, request.headers, app.state.official,
                )
            except ValueError:
                return card
            return dict(card, trackCount=len(rows))

        return await asyncio.gather(*(with_count(card) for card in cards))

    async def forward(request):
        path = request.url.path + ("?" + request.url.query if request.url.query else "")
        return await app.state.official.passthrough(
            request.method, path, request.headers, await request.body()
        )

    def track(guid):
        return app.state.store.get_track(guid)

    def guid_of(request):
        return (
            request.query_params.get("guid")
            or request.query_params.get("trackGUID")
            or request.query_params.get("trackGuid")
            or request.query_params.get("id")
            or request.query_params.get("trackId")
            or str(request.path_params.get("subpath") or "").split("/")[0].split(".")[0]
            or ""
        )

    def page_args(request, *, total=None):
        try:
            page = max(1, int(request.query_params.get("page", "1")))
            size = int(request.query_params.get("size", "50"))
            if total is not None and size <= 0:
                # Native playlist clients use -1 to request the complete list.
                size = max(total, 1) if size == -1 else 50
            else:
                size = min(100, max(1, size))
        except ValueError:
            raise HTTPException(400, "分页参数无效") from None
        return page, size

    @app.get("/_ext/healthz")
    async def health():
        return {
            "status": "running",
            "mode": config.play_mode,
            "audio_cache": False,
            "client_failure_fallback": "requires-client-support",
        }

    @app.get("/_ext/jobs")
    @app.get(PREFIX + "_ext/jobs")
    async def jobs(request: Request):
        user = await identity(request)
        return envelope(app.state.store.jobs(user))

    @app.post("/_ext/jobs/retry")
    @app.post(PREFIX + "_ext/jobs/retry")
    async def retry(request: Request):
        user = await identity(request)
        body = await request.json()
        guid = str(body.get("guid") or "")
        if not app.state.store.is_liked(user, guid):
            raise HTTPException(404, "当前用户没有该收藏")
        app.state.store.update_job(
            user, guid, state="pending", attempts=0, next_at=0, error=""
        )
        return envelope(None)

    async def search_impl(request: Request):
        user = await identity(request)
        page, size = page_args(request)
        keyword = (
            request.query_params.get("keyword") or request.query_params.get("q") or ""
        )
        if not keyword.strip():
            return envelope(
                await app.state.official.search(keyword, request.headers, page, size)
            )
        key = (user, keyword)
        cached = search_cache.get(key)
        request.state.search_cached = bool(cached and cached[0] >= time.monotonic())
        if not cached or cached[0] < time.monotonic():
            local_ok, online_ok = True, True
            try:
                local = await app.state.official.list_all(
                    PREFIX + "search/track", request.headers, q=keyword
                )
            except (httpx.HTTPError, ValueError) as exc:
                local_ok = False
                local = {"list": []}
                event(
                    config.data_dir,
                    "proxy",
                    "search.local_failed",
                    level="ERROR",
                    error=exc,
                )
            try:
                raws = await app.state.lx.search(keyword, 1, 100)
            except (httpx.HTTPError, ValueError, RuntimeError) as exc:
                online_ok = False
                raws = []
                event(
                    config.data_dir,
                    "proxy",
                    "search.online_failed",
                    level="ERROR",
                    error=exc,
                )
            if not local_ok and not online_ok:
                raise ValueError("Both search services unavailable")
            online = []
            for raw in raws:
                try:
                    online.append(to_official(app.state.store.save_track(raw)))
                except (ValueError, TypeError, OverflowError) as exc:
                    event(
                        config.data_dir,
                        "proxy",
                        "search.row_skipped",
                        level="WARNING",
                        error=exc,
                    )
            unique = {row["guid"]: row for row in online + local["list"]}
            cached = (time.monotonic() + 60, list(unique.values()))
            if len(search_cache) >= 100:
                search_cache.pop(next(iter(search_cache)))
            search_cache[key] = cached
        online = [dict(row) for row in cached[1]]
        for vo in online:
            if track(vo["guid"]):
                vo["isFavorite"] = app.state.store.is_liked(user, vo["guid"])
        return envelope(
            {"list": online[(page - 1) * size : page * size], "total": len(online)}
        )

    @app.get(PREFIX + "search/track")
    @app.get(PREFIX + "search/track/{subpath:path}")
    async def search(request: Request):
        started = time.monotonic()
        keyword = (
            request.query_params.get("keyword") or request.query_params.get("q") or ""
        )
        try:
            result = await search_impl(request)
            data = result.get("data") or {}
            rows = data.get("list", [])
            activity(
                config.data_dir,
                "search.completed",
                user=request.state.music_user,
                keyword=keyword,
                total=data.get("total", len(rows)),
                returned=len(rows),
                cache_hit=getattr(request.state, "search_cached", False),
                duration_ms=round((time.monotonic() - started) * 1000),
            )
            return result
        except Exception as exc:
            user = getattr(request.state, "music_user", None)
            if user is not None:
                activity(
                    config.data_dir,
                    "search.failed",
                    user=user,
                    keyword=keyword,
                    level="ERROR",
                    error=exc,
                    duration_ms=round((time.monotonic() - started) * 1000),
                )
            raise

    def first_play_request(request):
        interval = request.headers.get("range", "")
        return request.method == "GET" and (
            not interval or interval.startswith("bytes=0-")
        )

    def playback_note(name, request, item, user=None, **fields):
        if user is None:
            user = known_user(request)
        activity(
            config.data_dir,
            name,
            user=user,
            track=item,
            route=safe_route(request.url.path),
            method=request.method,
            **fields,
        )

    def native_track(guid, request):
        user = known_user(request)
        native = native_items.get((user, guid))
        rows_to_check = []
        if native and native[0] > time.monotonic():
            rows_to_check.append((native[0], [native[1]]))
        rows_to_check.extend(v for k, v in search_cache.items() if user is not None and k[0] == user)
        for expires, rows in rows_to_check:
            if expires < time.monotonic():
                continue
            for row in rows:
                if row.get("guid") == guid:
                    artists = row.get("artists") or []
                    return {
                        "guid": guid,
                        "title": row.get("title") or row.get("name") or "官方曲库歌曲",
                        "artist": "、".join(
                            str(a.get("name") or "")
                            for a in artists
                            if isinstance(a, dict)
                        ),
                        "source": "official",
                    }
        safe_id = (
            guid
            if len(guid) == 32 and all(c in "0123456789abcdefABCDEF" for c in guid)
            else ""
        )
        return {
            "guid": safe_id,
            "title": "官方曲库歌曲（未取得曲目信息）",
            "source": "official",
        }

    @app.api_route(PREFIX + "track/stream", methods=["GET", "HEAD"])
    @app.api_route(PREFIX + "track/stream/{subpath:path}", methods=["GET", "HEAD"])
    async def stream(request: Request):
        item = track(guid_of(request))
        record = first_play_request(request)
        if not item:
            item = native_track(guid_of(request), request)
            try:
                response = await forward(request)
                if record and response.status_code < 400:
                    playback_note(
                        "play.local_forwarded",
                        request,
                        item,
                        mode="official",
                        status=response.status_code,
                    )
                return response
            except Exception as exc:
                if record:
                    playback_note(
                        "play.failed",
                        request,
                        item,
                        level="ERROR",
                        error=exc,
                        mode="official",
                    )
                raise
        user = await identity(request)
        if record:
            playback_note("play.requested", request, item, user, mode=config.play_mode)
        started = time.monotonic()
        try:
            response = await app.state.media.play(
                item,
                user,
                request.headers,
                request.headers.get("range"),
                request.method == "HEAD",
            )
            if record:
                mode = getattr(response, "fnmusic_mode", "proxy")
                outcome = (
                    "play.redirect"
                    if mode == "direct"
                    else "play.local_forwarded"
                    if mode == "local"
                    else "play.proxy_started"
                )
                playback_note(
                    outcome if response.status_code < 400 else "play.failed",
                    request,
                    item,
                    user,
                    level="INFO" if response.status_code < 400 else "ERROR",
                    mode=mode,
                    quality=getattr(response, "fnmusic_quality", ""),
                    status=response.status_code,
                    duration_ms=round((time.monotonic() - started) * 1000),
                )
            return response
        except Exception as exc:
            if record:
                playback_note(
                    "play.failed",
                    request,
                    item,
                    user,
                    level="ERROR",
                    error=exc,
                    duration_ms=round((time.monotonic() - started) * 1000),
                )
            raise

    @app.get(PREFIX + "track/lyrics")
    @app.get(PREFIX + "track/lyrics/{subpath:path}")
    @app.get(PREFIX + "detail/lyrics/{subpath:path}")
    async def lyrics(request: Request):
        item = track(guid_of(request))
        if not item:
            return await forward(request)
        await identity(request)
        data = await app.state.lx.lyric(item["raw"])
        if isinstance(data, dict) and isinstance(data.get("promise"), dict):
            data = data["promise"]
        return envelope(
            {
                "guid": item["guid"],
                "lyric": data.get("lyric", ""),
                "tlyric": data.get("tlyric", ""),
                "rlyric": data.get("rlyric", ""),
            }
        )

    @app.api_route(PREFIX + "track/transcode", methods=["GET", "POST"])
    @app.api_route(PREFIX + "track/transcode/heartbeat", methods=["GET", "POST"])
    @app.api_route(PREFIX + "track/transcode/quit", methods=["GET", "POST"])
    async def transcode(request: Request):
        body = await request.json() if request.method == "POST" else {}
        guid = guid_of(request) or body.get("trackGUID") or body.get("guid") or ""
        if not track(guid):
            return await forward(request)
        await identity(request)
        return envelope({"guid": guid, "status": "ready"})

    @app.api_route(PREFIX + "track/hls/{guid}/{filename}", methods=["GET", "HEAD"])
    async def hls(request: Request, guid: str, filename: str):
        item = track(guid)
        if not item:
            record = request.method == "GET" and filename.endswith(".m3u8")
            try:
                response = await forward(request)
                if record and response.status_code < 400:
                    playback_note(
                        "play.playlist_forwarded",
                        request,
                        native_track(guid, request),
                        mode="official_hls",
                        status=response.status_code,
                    )
                return response
            except Exception as exc:
                if record:
                    playback_note(
                        "play.failed",
                        request,
                        native_track(guid, request),
                        mode="official_hls",
                        level="ERROR",
                        error=exc,
                    )
                raise
        user = await identity(request)
        from starlette.responses import Response

        if filename == "preset.m3u8":
            body = app.state.transcode.playlist(item)
            return Response(
                b"" if request.method == "HEAD" else body,
                media_type="application/vnd.apple.mpegurl",
                headers={"Cache-Control": "no-store"},
            )
        if filename == "audio.aac":
            if request.method == "HEAD":
                return Response(media_type="audio/aac")
            record = first_play_request(request)
            if record:
                playback_note("play.requested", request, item, user, mode="hls")
            try:
                response = await app.state.transcode.audio(item, user, request.headers)
                if record:
                    playback_note(
                        "play.hls_started",
                        request,
                        item,
                        user,
                        mode="hls",
                        quality="aac_128k",
                        status=response.status_code,
                    )
                return response
            except Exception as exc:
                if record:
                    playback_note(
                        "play.failed",
                        request,
                        item,
                        user,
                        mode="hls",
                        level="ERROR",
                        error=exc,
                    )
                raise
        raise HTTPException(404)

    @app.get(PREFIX + "track/metadata")
    @app.get(PREFIX + "track/metadata/{subpath:path}")
    @app.get(PREFIX + "track/audio-info")
    async def metadata(request: Request):
        item = track(guid_of(request))
        if not item:
            return await forward(request)
        user = await identity(request)
        vo = to_official(item, app.state.store.is_liked(user, item["guid"]))
        return envelope(dict(vo, track=dict(vo)))

    @app.api_route(PREFIX + "static/cover", methods=["GET", "HEAD"])
    @app.api_route(PREFIX + "static/cover/{subpath:path}", methods=["GET", "HEAD"])
    async def cover(request: Request):
        guid = (
            request.query_params.get("id")
            or request.query_params.get("coverId")
            or guid_of(request)
        )
        artwork = playlist_cover_path(guid)
        if artwork:
            await identity(request)
            return FileResponse(
                artwork, media_type="image/png",
                headers={"Cache-Control": "private, max-age=86400"},
            )
        item = track(guid)
        if not item:
            return await forward(request)
        await identity(request)
        # Covers use their own image validator; audio-only open_media is deliberately not reused.
        return await image_response(
            app.state.media.client, item["cover"], request.method == "HEAD"
        )

    @app.post(PREFIX + "favorite-track/create")
    async def like(request: Request):
        body = await request.json()
        guid = str(body.get("trackGUID") or body.get("guid") or "")
        if not track(guid):
            return await forward(request)
        user = await identity(request)
        app.state.store.like(user, guid)
        return envelope(None)

    @app.post(PREFIX + "favorite-track/delete")
    async def unlike(request: Request):
        body = await request.json()
        guid = str(body.get("trackGUID") or body.get("guid") or "")
        user = await identity(request)
        if not track(guid):
            bound = [
                j for j in app.state.store.jobs(user) if j["official_guid"] == guid
            ]
            if not bound:
                return await forward(request)
            # The App now displays the official GUID; clear each matching user intent.
            await app.state.official.api(
                "POST",
                PREFIX + "favorite-track/delete",
                request.headers,
                json={"trackGUID": guid},
            )
            for job in bound:
                app.state.store.unlike(user, job["guid"])
            return envelope(None)
        job = next((j for j in app.state.store.jobs(user) if j["guid"] == guid), None)
        app.state.store.unlike(user, guid)
        if job and job["official_guid"]:
            await app.state.official.api(
                "POST",
                PREFIX + "favorite-track/delete",
                request.headers,
                json={"trackGUID": job["official_guid"]},
            )
        return envelope(None)

    @app.get(PREFIX + "favorite-track/list")
    async def favorites(request: Request):
        user = await identity(request)
        page, size = page_args(request)
        data = await app.state.official.list_all(
            PREFIX + "favorite-track/list", request.headers
        )
        data = data or {"list": [], "total": 0}
        bound = {j["guid"] for j in app.state.store.jobs(user) if j["state"] == "done"}
        online = [
            to_official(t, True)
            for t in app.state.store.favorites(user)
            if t["guid"] not in bound
        ]
        rows = online + data.get("list", [])
        return envelope(
            dict(data, list=rows[(page - 1) * size : page * size], total=len(rows))
        )

    @app.get(PREFIX + "playlist/list")
    @app.get(PREFIX + "playlist/list/{subpath:path}")
    async def playlist_list(request: Request):
        user = await identity(request)
        page, size = page_args(request)
        data = await app.state.official.list_all(
            PREFIX + "playlist/list", request.headers
        )
        data = data or {"list": [], "total": 0}
        rows = app.state.playlists.cards(user) + app.state.playlists.official_covers(data.get("list", []))
        cards = await discovery_card_counts(request, user, rows[(page - 1) * size : page * size])
        return envelope(dict(data, list=cards, total=len(rows)))

    @app.get(PREFIX + "playlist/detail")
    async def playlist_detail(request: Request):
        user = await identity(request)
        guid = guid_of(request)
        card = next(
            (c for c in app.state.playlists.cards(user) if c["guid"] == guid), None
        )
        if not card:
            data = await app.state.official.api(
                "GET", PREFIX + "playlist/detail", request.headers,
                params=request.query_params,
            )
            return envelope(app.state.playlists.official_covers(data))
        rows = await playlist_rows(request, user, guid)
        return envelope(dict(card, trackCount=len(rows)))

    @app.get(PREFIX + "playlist/batch-detail")
    async def playlist_batch(request: Request):
        user = await identity(request)
        guids = [
            g.strip()
            for g in (request.query_params.get("guids") or guid_of(request)).split(",")
            if g.strip()
        ]
        cards = {row["guid"]: row for row in app.state.playlists.cards(user)}
        online = [cards[g] for g in guids if g in cards]
        rest = [g for g in guids if g not in cards]
        if not online:
            data = await app.state.official.api(
                "GET", PREFIX + "playlist/batch-detail", request.headers,
                params=request.query_params,
            )
            return envelope(app.state.playlists.official_covers(data))
        official = []
        if rest:
            data = await app.state.official.api(
                "GET",
                PREFIX + "playlist/batch-detail",
                request.headers,
                params={"guids": ",".join(rest)},
            )
            data = app.state.playlists.official_covers(data)
            official = data if isinstance(data, list) else (data or {}).get("list", [])
        online = await discovery_card_counts(request, user, online)
        return envelope({"list": online + official})

    @app.get(PREFIX + "track/playlist-detail/list")
    async def playlist_tracks(request: Request):
        user = await identity(request)
        guid = (
            request.query_params.get("playlistGUID")
            or request.query_params.get("playlistGuid")
            or guid_of(request)
        )
        try:
            app.state.playlists.key(user, guid)
        except KeyError:
            return await forward(request)
        rows = await playlist_rows(request, user, guid)
        page, size = page_args(request, total=len(rows))
        return envelope(
            {
                "list": [
                    (t if app.state.playlists.key(user, guid) == "archaeology"
                     else to_official(t, app.state.store.is_liked(user, t["guid"])))
                    for t in rows[(page - 1) * size : page * size]
                ],
                "total": len(rows),
            }
        )

    @app.api_route(
        "/{path:path}",
        methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    )
    async def passthrough(request: Request, path: str):
        return await forward(request)

    return app
