import discord
from discord.ext import commands
import yt_dlp
import asyncio
import aiohttp
import re
import os
import logging
from collections import deque
from dataclasses import dataclass, field
from dotenv import load_dotenv

# ══════════════════════════════════════════════════════
#  CONFIGURACIÓN  ← edita solo esta sección
# ══════════════════════════════════════════════════════
load_dotenv()
TOKEN           = os.getenv("DISCORD_TOKEN")
ALLOWED_CHANNEL = int(os.getenv("ALLOWED_CHANNEL", "0"))
INACTIVITY_TIMEOUT = 300  # segundos (5 min) sin reproducir → desconectar
# ══════════════════════════════════════════════════════

# ─────────────────────────────────────────
#  LOGGING
# ─────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("MusicBot")

intents = discord.Intents.default()
intents.message_content = True


class MusicBot(commands.Bot):
    async def close(self):
        global http_session
        if http_session and not http_session.closed:
            await http_session.close()
            log.info("Sesión HTTP cerrada.")
        await super().close()


bot = MusicBot(command_prefix="p ", intents=intents, help_command=None)

# Sesión HTTP global (se inicializa en on_ready)
http_session: aiohttp.ClientSession | None = None

# ─────────────────────────────────────────
#  RECORDATORIO DE COMANDOS
# ─────────────────────────────────────────
COMMANDS_HELP = (
    "```\n"
    "📋 Comandos disponibles (prefijo: p )\n"
    "──────────────────────────────────────\n"
    "p +<url>          → Reproduce un video o playlist de YouTube\n"
    "p next <url>      → Reproduce esta canción después de la actual\n"
    "p skip            → Salta la canción actual\n"
    "p pause           → Pausa la reproducción\n"
    "p resume          → Reanuda la reproducción\n"
    "p stop            → Detiene la música y desconecta el bot\n"
    "p queue           → Muestra la cola actual\n"
    "p np              → Muestra la canción que suena ahora\n"
    "p loop            → Activa/desactiva el bucle de la pista actual\n"
    "p letra           → Muestra la letra de la canción actual\n"
    "p vol <0-100>     → Ajusta el volumen\n"
    "p help            → Muestra este mensaje\n"
    "```"
)

# ─────────────────────────────────────────
#  OPCIONES DE YT-DLP
# ─────────────────────────────────────────
YDL_FLAT = {
    "format": "bestaudio/best",
    "noplaylist": False,
    "quiet": True,
    "no_warnings": True,
    "extract_flat": "in_playlist",
    "default_search": "auto",
}

YDL_FULL = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "extract_flat": False,
}

FFMPEG_OPTIONS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn -af aresample=async=1",
}

# ─────────────────────────────────────────
#  ESTADO POR SERVIDOR (encapsulado)
# ─────────────────────────────────────────
@dataclass
class GuildState:
    queue: deque = field(default_factory=deque)
    now_playing: dict | None = None
    loop: bool = False
    volume: float = 0.30
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _inactivity_task: asyncio.Task | None = None

guild_states: dict[int, GuildState] = {}

def get_state(guild_id: int) -> GuildState:
    if guild_id not in guild_states:
        guild_states[guild_id] = GuildState()
    return guild_states[guild_id]


# ─────────────────────────────────────────
#  GUARD: solo canal permitido
# ─────────────────────────────────────────
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    if message.channel.id != ALLOWED_CHANNEL:
        return
    await bot.process_commands(message)


# ─────────────────────────────────────────
#  HELPER: conexión al canal de voz
# ─────────────────────────────────────────
async def ensure_voice(ctx: commands.Context) -> discord.VoiceClient | None:
    """Conecta o mueve el bot al canal de voz del usuario. Retorna None si falla."""
    if not ctx.author.voice or not ctx.author.voice.channel:
        await ctx.send("❌ Debes estar en un canal de voz primero.")
        return None
    channel = ctx.author.voice.channel
    vc: discord.VoiceClient = ctx.voice_client
    if vc is None:
        return await channel.connect()
    if vc.channel != channel:
        await vc.move_to(channel)
    return vc


# ─────────────────────────────────────────
#  TIMER DE INACTIVIDAD
# ─────────────────────────────────────────
def _cancel_inactivity(state: GuildState):
    """Cancela el timer de inactividad si existe."""
    if state._inactivity_task and not state._inactivity_task.done():
        state._inactivity_task.cancel()
        state._inactivity_task = None


async def _inactivity_disconnect(guild_id: int, voice_client: discord.VoiceClient, channel: discord.TextChannel):
    """Espera INACTIVITY_TIMEOUT segundos y desconecta si sigue inactivo."""
    try:
        await asyncio.sleep(INACTIVITY_TIMEOUT)
        state = get_state(guild_id)
        if voice_client.is_connected() and not voice_client.is_playing() and not voice_client.is_paused():
            state.now_playing = None
            state.queue.clear()
            state.loop = False
            await voice_client.disconnect()
            await channel.send(f"💤 **Desconectado por inactividad** ({INACTIVITY_TIMEOUT // 60} min sin reproducir).")
            log.info("Desconectado por inactividad en guild %s", guild_id)
    except asyncio.CancelledError:
        pass


def _start_inactivity_timer(guild_id: int, voice_client: discord.VoiceClient, channel: discord.TextChannel):
    """Inicia (o reinicia) el timer de inactividad."""
    state = get_state(guild_id)
    _cancel_inactivity(state)
    state._inactivity_task = asyncio.create_task(
        _inactivity_disconnect(guild_id, voice_client, channel)
    )


# ─────────────────────────────────────────
#  LETRAS — via LRCLib API (gratis, sin API key)
# ─────────────────────────────────────────
def _clean_title(title: str) -> str:
    """Limpia el título de YouTube para mejorar la búsqueda."""
    title = re.sub(r"\(.*?\)|\[.*?\]", "", title)
    title = re.sub(
        r"\b(official|video|audio|lyric|lyrics|topic|vevo|hd|4k|live|version|mv)\b",
        "", title, flags=re.IGNORECASE,
    )
    title = re.sub(r"\s{2,}", " ", title)
    return title.strip(" -–|")


def _strip_lrc_timestamps(lrc: str) -> str:
    """Convierte formato LRC [mm:ss.xx] en texto plano."""
    lines = []
    for line in lrc.splitlines():
        clean = re.sub(r"\[\d{1,2}:\d{2}(?:\.\d+)?\]", "", line).strip()
        if clean:
            lines.append(clean)
    deduped = []
    for line in lines:
        if not deduped or deduped[-1] != line:
            deduped.append(line)
    return "\n".join(deduped)


async def fetch_lyrics(title: str) -> str | None:
    """
    Busca la letra en LRCLib (API pública, sin key).
    Reutiliza la sesión HTTP global para eficiencia.
    """
    if not title or "videoplayback" in title.lower():
        return None

    query = _clean_title(title)
    if len(query) < 3:
        return None

    log.info("Buscando letra: '%s'", query)

    try:
        async with http_session.get(
            "https://lrclib.net/api/search",
            params={"q": query},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status != 200:
                log.warning("LRCLib respondió %d", resp.status)
                return None
            results = await resp.json()

        if not results:
            log.info("Sin resultados de letra para '%s'", query)
            return None

        for entry in results:
            synced = entry.get("syncedLyrics") or ""
            plain  = entry.get("plainLyrics") or ""
            if synced:
                return _strip_lrc_timestamps(synced).strip() or None
            if plain:
                return plain.strip() or None

        return None

    except Exception as e:
        log.error("Error buscando letra: %s", e)
        return None


async def post_lyrics(channel: discord.TextChannel, title: str):
    """Publica la letra dividida en mensajes de máx 1900 chars."""
    lyrics = await fetch_lyrics(title)

    if not lyrics:
        await channel.send(f"📄 No encontré la letra de **{title}**.")
        return

    header = f"📄 **Letra de:** `{title}`\n{'─' * 40}\n"
    MAX = 1900
    chunks = []
    current = header

    for line in lyrics.splitlines():
        candidate = current + line + "\n"
        if len(candidate) > MAX:
            chunks.append(current)
            current = line + "\n"
        else:
            current = candidate

    if current.strip():
        chunks.append(current)

    for chunk in chunks:
        await channel.send(chunk)


# ─────────────────────────────────────────
#  HELPERS DE AUDIO
# ─────────────────────────────────────────
async def extract_info(url: str) -> list[dict]:
    """Devuelve lista de {title, url} sin descargar nada."""
    loop = asyncio.get_running_loop()

    def _extract():
        with yt_dlp.YoutubeDL(YDL_FLAT) as ydl:
            info = ydl.extract_info(url, download=False)
            tracks = []
            if "entries" in info:
                for entry in info["entries"]:
                    if not entry:
                        continue
                    webpage = (
                        entry.get("webpage_url")
                        or entry.get("url")
                        or f"https://www.youtube.com/watch?v={entry.get('id', '')}"
                    )
                    tracks.append({
                        "title": entry.get("title") or "Desconocido",
                        "url": webpage,
                    })
            else:
                webpage = info.get("webpage_url") or info.get("url") or url
                tracks.append({
                    "title": info.get("title") or "Desconocido",
                    "url": webpage,
                })
            return tracks

    return await loop.run_in_executor(None, _extract)


async def get_stream_url(track: dict) -> tuple[str, str]:
    """Devuelve (stream_url, título limpio) para un track."""
    loop = asyncio.get_running_loop()
    stored_title = track["title"]

    def _get():
        with yt_dlp.YoutubeDL(YDL_FULL) as ydl:
            info = ydl.extract_info(track["url"], download=False)
            stream    = info.get("url", "")
            yt_title  = info.get("title") or ""
            final_title = (
                yt_title
                if yt_title and "videoplayback" not in yt_title.lower()
                else stored_title
            )
            return stream, final_title

    return await loop.run_in_executor(None, _get)


async def play_next(guild_id: int, voice_client: discord.VoiceClient, channel: discord.TextChannel):
    """Reproduce la siguiente pista de la cola."""
    state = get_state(guild_id)

    async with state._lock:
        if not state.queue:
            state.now_playing = None
            await channel.send("✅ **Cola vacía. ¡Hasta la próxima!**")
            _start_inactivity_timer(guild_id, voice_client, channel)
            return

        track = state.queue.popleft()

    _cancel_inactivity(state)

    try:
        # Detectar si es repeat de loop ANTES de actualizar now_playing
        is_loop_repeat = state.loop and state.now_playing is not None

        stream_url, title = await get_stream_url(track)
        state.now_playing = {"title": title, "url": track["url"]}

        source = discord.FFmpegPCMAudio(stream_url, **FFMPEG_OPTIONS)
        source = discord.PCMVolumeTransformer(source, volume=state.volume)

        def after_playing(error):
            if error:
                log.error("Error en reproducción: %s", error)
            if state.loop:
                state.queue.appendleft(track)
            asyncio.run_coroutine_threadsafe(
                play_next(guild_id, voice_client, channel), bot.loop
            )

        voice_client.play(source, after=after_playing)

        if not is_loop_repeat:
            view = LyricsButton(title, channel)
            await channel.send(f"🎵 **Reproduciendo:** `{title}`", view=view)

    except Exception as e:
        await channel.send(f"⚠️ Error al reproducir `{track['title']}`: {e}")
        await play_next(guild_id, voice_client, channel)


# ─────────────────────────────────────────
#  BOTÓN DE LETRA
# ─────────────────────────────────────────
class LyricsButton(discord.ui.View):
    """View con botón que postea la letra (un solo uso)."""

    def __init__(self, title: str, channel: discord.TextChannel):
        super().__init__(timeout=600)
        self.title   = title
        self.channel = channel

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True

    @discord.ui.button(label="📄 Ver letra", style=discord.ButtonStyle.secondary)
    async def show_lyrics(self, interaction: discord.Interaction, button: discord.ui.Button):
        button.disabled = True
        button.label    = "📄 Buscando letra..."
        await interaction.response.edit_message(view=self)

        await post_lyrics(self.channel, self.title)

        button.label = "✅ Letra enviada"
        await interaction.edit_original_response(view=self)


# ─────────────────────────────────────────
#  COMANDOS
# ─────────────────────────────────────────

@bot.command(name="+")
async def play(ctx: commands.Context, *, url: str):
    """p +<url>  →  Reproduce un video o playlist de YouTube."""
    vc = await ensure_voice(ctx)
    if vc is None:
        return

    guild_id = ctx.guild.id

    async with ctx.typing():
        try:
            tracks = await extract_info(url)
        except Exception as e:
            await ctx.send(f"⚠️ No pude obtener el audio: `{e}`")
            return

    state = get_state(guild_id)

    if len(tracks) == 1:
        state.queue.append(tracks[0])
        if not vc.is_playing():
            await play_next(guild_id, vc, ctx.channel)
        else:
            await ctx.send(f"➕ **Agregado a la cola:** `{tracks[0]['title']}`")
    else:
        state.queue.extend(tracks)
        await ctx.send(f"📋 **Playlist agregada:** `{len(tracks)}` pistas en la cola.")
        if not vc.is_playing():
            await play_next(guild_id, vc, ctx.channel)


@bot.command(name="next")
async def play_next_cmd(ctx: commands.Context, *, url: str):
    """p next <url>  →  Inserta una canción al frente de la cola."""
    vc = await ensure_voice(ctx)
    if vc is None:
        return

    guild_id = ctx.guild.id

    async with ctx.typing():
        try:
            tracks = await extract_info(url)
        except Exception as e:
            await ctx.send(f"⚠️ No pude obtener el audio: `{e}`")
            return

    state = get_state(guild_id)

    for track in reversed(tracks):
        state.queue.appendleft(track)

    if not vc.is_playing():
        await play_next(guild_id, vc, ctx.channel)
    else:
        if len(tracks) == 1:
            await ctx.send(
                f"⏩ **Siguiente en reproducir:** `{tracks[0]['title']}`\n"
                f"*(Continúa la playlist después)*"
            )
        else:
            await ctx.send(
                f"⏩ **{len(tracks)} pistas insertadas al frente de la cola.**\n"
                f"*(Continúa la playlist después)*"
            )


@bot.command(name="skip")
async def skip(ctx: commands.Context):
    vc = ctx.voice_client
    if vc and vc.is_playing():
        get_state(ctx.guild.id).loop = False
        vc.stop()
        await ctx.send("⏭️ **Pista saltada.**")
    else:
        await ctx.send("❌ No hay nada reproduciéndose.")


@bot.command(name="pause")
async def pause(ctx: commands.Context):
    """p pause  →  Pausa la reproducción actual."""
    vc = ctx.voice_client
    if vc and vc.is_playing():
        vc.pause()
        await ctx.send("⏸️ **Reproducción pausada.** Usá `p resume` para continuar.")
    elif vc and vc.is_paused():
        await ctx.send("⏸️ Ya está pausado. Usá `p resume` para continuar.")
    else:
        await ctx.send("❌ No hay nada reproduciéndose.")


@bot.command(name="resume")
async def resume(ctx: commands.Context):
    """p resume  →  Reanuda la reproducción pausada."""
    vc = ctx.voice_client
    if vc and vc.is_paused():
        vc.resume()
        await ctx.send("▶️ **Reproducción reanudada.**")
    elif vc and vc.is_playing():
        await ctx.send("▶️ Ya se está reproduciendo.")
    else:
        await ctx.send("❌ No hay nada pausado.")


@bot.command(name="loop")
async def loop_cmd(ctx: commands.Context):
    """p loop  →  Activa/desactiva el bucle de la pista actual."""
    state = get_state(ctx.guild.id)
    state.loop = not state.loop
    icon   = "🔁" if state.loop else "➡️"
    status = "activado" if state.loop else "desactivado"
    extra  = f" — `{state.now_playing['title']}`" if state.loop and state.now_playing else ""
    await ctx.send(f"{icon} **Bucle {status}.**{extra}")


@bot.command(name="stop")
async def stop(ctx: commands.Context):
    vc = ctx.voice_client
    if vc:
        state = get_state(ctx.guild.id)
        _cancel_inactivity(state)
        state.queue.clear()
        state.now_playing = None
        state.loop        = False
        vc.stop()
        await vc.disconnect()
        await ctx.send("⏹️ **Música detenida y bot desconectado.**")
    else:
        await ctx.send("❌ El bot no está en ningún canal.")


@bot.command(name="queue")
async def queue_cmd(ctx: commands.Context):
    state = get_state(ctx.guild.id)

    if not state.now_playing and not state.queue:
        await ctx.send("📭 La cola está vacía.")
        return

    lines = []
    if state.now_playing:
        lines.append(f"🎵 **Ahora:** `{state.now_playing['title']}`\n")
    if state.queue:
        lines.append("**Cola:**")
        for i, track in enumerate(list(state.queue)[:15], 1):
            lines.append(f"`{i}.` {track['title']}")
        if len(state.queue) > 15:
            lines.append(f"*... y {len(state.queue) - 15} pistas más*")

    await ctx.send("\n".join(lines))


@bot.command(name="np")
async def now_playing_cmd(ctx: commands.Context):
    current = get_state(ctx.guild.id).now_playing
    if current:
        await ctx.send(f"🎵 **Reproduciendo ahora:** `{current['title']}`")
    else:
        await ctx.send("❌ No hay nada reproduciéndose.")


@bot.command(name="letra")
async def lyrics_cmd(ctx: commands.Context):
    """p letra  →  Muestra la letra de la canción actual."""
    current = get_state(ctx.guild.id).now_playing
    if not current:
        await ctx.send("❌ No hay nada reproduciéndose.")
        return
    await ctx.send(f"🔍 Buscando letra de `{current['title']}`...")
    await post_lyrics(ctx.channel, current["title"])


@bot.command(name="vol")
async def volume(ctx: commands.Context, vol: int):
    vc = ctx.voice_client
    if not vc or not vc.is_playing():
        await ctx.send("❌ No hay nada reproduciéndose.")
        return
    if not (0 <= vol <= 100):
        await ctx.send("❌ El volumen debe estar entre 0 y 100.")
        return
    if not isinstance(vc.source, discord.PCMVolumeTransformer):
        await ctx.send("❌ No se puede ajustar el volumen en este momento.")
        return

    state = get_state(ctx.guild.id)
    state.volume = vol / 100
    vc.source.volume = state.volume
    await ctx.send(f"🔊 Volumen ajustado a **{vol}%**")


@bot.command(name="help")
async def help_cmd(ctx: commands.Context):
    """p help  →  Muestra los comandos disponibles."""
    await ctx.send(COMMANDS_HELP)


# ─────────────────────────────────────────
#  EVENTOS
# ─────────────────────────────────────────
@bot.event
async def on_ready():
    global http_session
    if http_session is None or http_session.closed:
        http_session = aiohttp.ClientSession()
    log.info("Bot conectado como %s (ID: %s)", bot.user, bot.user.id)
    if ALLOWED_CHANNEL == 0:
        log.warning(
            "ALLOWED_CHANNEL vacío o 0: no voy a leer comandos. "
            "Poné el ID del canal en .env (ALLOWED_CHANNEL=...)."
        )
    else:
        log.info("Comandos solo en el canal %s", ALLOWED_CHANNEL)
    await bot.change_presence(activity=discord.Activity(
        type=discord.ActivityType.listening, name="p +<url>"
    ))


@bot.event
async def on_guild_remove(guild: discord.Guild):
    state = guild_states.pop(guild.id, None)
    if state:
        _cancel_inactivity(state)


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("❌ Uso correcto: `p +<url de YouTube>` | Escribí `p help` para ver todos los comandos.")
    elif isinstance(error, (commands.CommandNotFound, commands.CheckFailure)):
        pass
    else:
        log.exception("Error no manejado en comando '%s': %s", ctx.command, error)


if __name__ == "__main__":
    if not TOKEN:
        raise ValueError("❌ DISCORD_TOKEN no encontrado. Creá un archivo .env con DISCORD_TOKEN=tu_token")
    bot.run(TOKEN)