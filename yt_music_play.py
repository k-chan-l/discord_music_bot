import asyncio
import logging
import re
from collections import deque
from enum import Enum, auto

import yt_dlp
import discord
from discord.ext import commands
from discord import app_commands

logger = logging.getLogger(__name__)

# ── 유틸 ──────────────────────────────────────────────────────────────────────

def is_url(text: str) -> bool:
    return re.match(r"https?://", text) is not None


YTDL_OPTS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
}

FFMPEG_OPTS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}


# ── 데이터 ────────────────────────────────────────────────────────────────────

class PlayerState(Enum):
    '''
    상태 전이
    IDLE  ──재생──►  PLAYING  ──일시정지──►  PAUSED
      ▲                │                      │
      └──나가──────────┘◄─────────────────────┘
    IDLE  ◄──타임아웃──  WAITING  ◄──큐 소진──  PLAYING
    '''
    IDLE    = auto()
    WAITING = auto()
    PLAYING = auto()
    PAUSED  = auto()


class Song:
    def __init__(
        self,
        url: str,
        title: str,
        duration: int,
        thumbnail: str,
        requester: str,
        requester_id: int,
    ):
        self.url = url
        self.title = title
        self.duration = duration
        self.thumbnail = thumbnail
        self.requester = requester
        self.requester_id = requester_id


# ── yt-dlp 비동기 래퍼 ────────────────────────────────────────────────────────
# yt-dlp는 동기(sync) 라이브러리라 그냥 호출하면 이벤트 루프 전체가 멈춤.
# run_in_executor로 별도 스레드에서 실행해 봇이 멈추지 않게 함.

async def fetch_song(query: str, user: discord.User) -> "Song | None":
    """URL 또는 검색어로 Song 반환"""
    search = query if is_url(query) else f"ytsearch1:{query}"
    logger.info("곡 검색 시작 | 요청자: %s | 쿼리: %s", user.display_name, search)

    def extract():
        with yt_dlp.YoutubeDL(YTDL_OPTS) as ydl:
            return ydl.extract_info(search, download=False)

    loop = asyncio.get_running_loop()
    try:
        info = await loop.run_in_executor(None, extract)
        # 검색 결과는 entries 리스트, 직접 URL은 바로 info
        entry = info["entries"][0] if "entries" in info else info
        song = Song(
            url=entry["webpage_url"],
            title=entry["title"],
            duration=entry["duration"],
            thumbnail=entry["thumbnail"],
            requester=user.display_name,
            requester_id=user.id,
        )
        logger.info("곡 검색 완료 | 제목: %s | 길이: %d초", song.title, song.duration)
        return song
    except Exception as e:
        logger.error("곡 검색 실패 | 쿼리: %s | 오류: %s", search, repr(e))
        return None


async def fetch_audio(song: Song) -> "discord.FFmpegPCMAudio | None":
    """Song의 실제 스트리밍 URL을 가져와 FFmpegPCMAudio 반환"""
    logger.info("스트리밍 URL 추출 시작 | 제목: %s", song.title)

    def extract():
        with yt_dlp.YoutubeDL(YTDL_OPTS) as ydl:
            info = ydl.extract_info(song.url, download=False)
            return info["url"]

    loop = asyncio.get_running_loop()
    try:
        stream_url = await loop.run_in_executor(None, extract)
        logger.info("스트리밍 URL 추출 완료 | 제목: %s", song.title)
        return discord.FFmpegPCMAudio(stream_url, **FFMPEG_OPTS)
    except Exception as e:
        logger.error("스트리밍 URL 추출 실패 | 제목: %s | 오류: %s", song.title, repr(e))
        return None


# ── 큐 ───────────────────────────────────────────────────────────────────────

class SongQueue:
    def __init__(self):
        self._q: deque[Song] = deque()
        self.current: "Song | None" = None
        self.is_looping = False
        self.is_paused = False

    def reset(self):
        self._q.clear()
        self.current = None
        self.is_looping = False
        self.is_paused = False

    def add(self, song: Song):
        self._q.append(song)

    def add_front(self, song: Song):
        self._q.appendleft(song)

    def next(self) -> "Song | None":
        if self._q:
            self.current = self._q.popleft()
            return self.current
        self.current = None
        return None

    def __bool__(self):
        return bool(self._q)


# ── UI ───────────────────────────────────────────────────────────────────────

class MusicView(discord.ui.View):
    def __init__(self, music: "Music"):
        super().__init__(timeout=None)
        self.music = music
        if music.queue.is_looping:
            self.loop_button.style = discord.ButtonStyle.success
        if music.queue.is_paused:
            self.pause_button.style = discord.ButtonStyle.secondary

    @discord.ui.button(label="⏮️", style=discord.ButtonStyle.primary)
    async def prev_button(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.defer()
        self.music.rewind()

    @discord.ui.button(label="⏯️", style=discord.ButtonStyle.primary)
    async def pause_button(self, interaction: discord.Interaction, _: discord.ui.Button):
        self.music.pause_resume()
        await interaction.response.edit_message(view=MusicView(self.music))

    @discord.ui.button(label="⏭️", style=discord.ButtonStyle.primary)
    async def next_button(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.defer()
        self.music.skip()

    @discord.ui.button(label="🔁", style=discord.ButtonStyle.secondary)
    async def loop_button(self, interaction: discord.Interaction, _: discord.ui.Button):
        self.music.loop()
        await interaction.response.edit_message(view=MusicView(self.music))


# ── Cog ──────────────────────────────────────────────────────────────────────

class Music(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.queue = SongQueue()
        self.state = PlayerState.IDLE
        self.voice_client: "discord.VoiceClient | None" = None
        self.channel: "discord.TextChannel | None" = None
        self._ui_message: "discord.Message | None" = None
        self._ui_view: "MusicView | None" = None
        self._idle_task: "asyncio.Task | None" = None
        self._is_leaving = False
        logger.info("Music Cog 초기화 완료")

    # ── 타임아웃 ──────────────────────────────────────────────────────────────

    async def _idle_timeout(self, seconds: int):
        msg = None
        try:
            logger.info("유휴 타임아웃 시작 | %d초 후 퇴장", seconds)
            msg = await self.channel.send(embed=self._wait_embed(seconds // 60))
            await asyncio.sleep(seconds)
            logger.info("유휴 타임아웃 완료 | 음성 채널 퇴장")
            await self._do_leave()
            self._go_idle()
        except asyncio.CancelledError:
            logger.info("유휴 타임아웃 취소됨")
            if msg:
                await msg.delete()

    def _cancel_idle(self):
        if self._idle_task:
            self._idle_task.cancel()
            self._idle_task = None

    # ── 음성 채널 조작 ────────────────────────────────────────────────────────

    async def _do_join(self, channel: discord.VoiceChannel) -> bool:
        logger.info("음성 채널 참가 시도 | 채널: %s (id=%s)", channel.name, channel.id)
        try:
            await channel.connect()
            logger.info("음성 채널 참가 성공 | 채널: %s", channel.name)
            return True
        except Exception as e:
            logger.error("음성 채널 참가 실패 | 채널: %s | 오류: %s", channel.name, repr(e))
            return False

    async def _do_leave(self) -> bool:
        logger.info("음성 채널 퇴장 시도")
        try:
            await self.voice_client.disconnect()
            logger.info("음성 채널 퇴장 성공")
            return True
        except Exception as e:
            logger.error("음성 채널 퇴장 실패 | 오류: %s", repr(e))
            return False

    # ── 재생 제어 ─────────────────────────────────────────────────────────────

    def skip(self):
        logger.info("곡 스킵")
        if self.voice_client:
            self.voice_client.stop()

    def rewind(self):
        logger.info("이전 곡으로 이동")
        if self.voice_client and self.queue.current:
            self.queue.add_front(self.queue.current)
            self.voice_client.stop()

    def pause_resume(self):
        self.queue.is_paused = not self.queue.is_paused
        if self.queue.is_paused:
            logger.info("일시정지")
            self.state = PlayerState.PAUSED
            self.voice_client.pause()
            self._idle_task = asyncio.create_task(self._idle_timeout(600))
        else:
            logger.info("재개")
            self._cancel_idle()
            self.state = PlayerState.PLAYING
            self.voice_client.resume()

    def loop(self):
        self.queue.is_looping = not self.queue.is_looping
        logger.info("반복 재생 %s", "ON" if self.queue.is_looping else "OFF")

    # ── 상태 전이 ─────────────────────────────────────────────────────────────

    def _go_idle(self):
        logger.info("상태 전이: %s → IDLE", self.state.name)
        self.state = PlayerState.IDLE
        self.queue.reset()
        self._idle_task = None
        self.voice_client = None
        self.channel = None

    def _go_waiting(self):
        logger.info("상태 전이: %s → WAITING", self.state.name)
        self.state = PlayerState.WAITING
        self._idle_task = asyncio.create_task(self._idle_timeout(300))

    def _go_playing(self):
        logger.info("상태 전이: %s → PLAYING", self.state.name)
        self._cancel_idle()
        self.state = PlayerState.PLAYING

    # ── 재생 루프 ─────────────────────────────────────────────────────────────

    async def _play_song(self, player: discord.FFmpegPCMAudio, embed: discord.Embed):
        self._ui_view = MusicView(self)
        self._ui_message = await self.channel.send(embed=embed, view=self._ui_view)
        self.voice_client.play(
            player,
            # voice_client.play의 after 콜백은 별도 스레드에서 실행되므로
            # 코루틴을 직접 호출할 수 없고, run_coroutine_threadsafe로 이벤트 루프에 등록
            after=lambda e: asyncio.run_coroutine_threadsafe(
                self._after_play(e), self.bot.loop
            ),
        )

    async def _play_next(self):
        if self._is_leaving:
            return
        if self.queue.is_looping and self.queue.current:
            self.queue.add_front(self.queue.current)
        song = self.queue.next()
        if song is None:
            logger.info("큐 소진 | WAITING 상태로 전환")
            self._go_waiting()
            return
        logger.info("다음 곡 재생 | 제목: %s", song.title)
        player = await fetch_audio(song)
        if player is None:
            await self.channel.send("음원 획득 실패, 다음 곡으로 넘어갑니다.")
            await self._play_next()
            return
        self._go_playing()
        await self._play_song(player, self._play_embed(song))

    async def _after_play(self, error):
        if error:
            logger.error("재생 오류: %s", error)
        else:
            logger.info("재생 종료 | 제목: %s", self.queue.current.title if self.queue.current else "알 수 없음")
        if self._ui_view:
            self._ui_view.stop()
            self._ui_view = None
        if self._ui_message:
            await self._ui_message.delete()
            self._ui_message = None
        await self._play_next()

    # ── Embed 생성 ────────────────────────────────────────────────────────────

    def _play_embed(self, song: Song) -> discord.Embed:
        embed = discord.Embed(
            title="🎵 현재 재생중",
            description=f"[{song.title}]({song.url})",
            color=discord.Color.blue(),
        )
        embed.add_field(name="요청자", value=f"<@{song.requester_id}>", inline=True)
        embed.add_field(name="길이", value=f"{song.duration // 60}분 {song.duration % 60}초", inline=True)
        embed.set_thumbnail(url=song.thumbnail)
        return embed

    def _add_embed(self, song: Song) -> discord.Embed:
        embed = discord.Embed(title="✅ 큐에 추가됨", description=f"[{song.title}]({song.url})")
        embed.add_field(name="요청자", value=f"<@{song.requester_id}>")
        embed.set_thumbnail(url=song.thumbnail)
        return embed

    def _wait_embed(self, minutes: int) -> discord.Embed:
        embed = discord.Embed(
            title="😢 노래 재생중이 아니에요",
            description=f"{minutes}분 동안 노래 재생이 없으면 음성 채널에서 나갑니다.",
            color=discord.Color.orange(),
        )
        embed.set_footer(text="다시 사용하려면 새로운 노래를 추가하거나 다시 재생해주세요.")
        return embed

    # ── 슬래시 커맨드 ─────────────────────────────────────────────────────────

    @app_commands.command(name="재생", description="봇이 노래를 재생합니다")
    async def play(self, interaction: discord.Interaction, search: str):
        logger.info("/재생 명령어 | 사용자: %s | 입력: %s | 현재 상태: %s",
                    interaction.user.display_name, search, self.state.name)
        await interaction.response.defer()

        if interaction.user.voice is None:
            logger.warning("사용자가 음성 채널에 없음 | 사용자: %s", interaction.user.display_name)
            await interaction.followup.send("사용자가 음성 채널에 없습니다.")
            return

        song = await fetch_song(search, interaction.user)
        if song is None:
            await interaction.followup.send("노래를 찾을 수 없습니다.")
            return

        self.queue.add(song)

        # 이미 재생 중이거나 일시정지 상태면 큐에만 추가
        if self.state in (PlayerState.PLAYING, PlayerState.PAUSED):
            logger.info("큐에 추가 | 제목: %s", song.title)
            await interaction.followup.send(embed=self._add_embed(song))
            return

        voice_channel = interaction.user.voice.channel

        if self.state == PlayerState.IDLE:
            if not await self._do_join(voice_channel):
                await interaction.followup.send("음성 채널 참가 실패")
                return
            self.channel = interaction.channel
            self.voice_client = interaction.guild.voice_client
        elif self.state == PlayerState.WAITING:
            self._cancel_idle()

        song = self.queue.next()
        player = await fetch_audio(song)
        if player is None:
            await interaction.followup.send("음원 획득 실패.")
            return

        await interaction.delete_original_response()
        self._go_playing()
        await self._play_song(player, self._play_embed(song))

    @app_commands.command(name="나가", description="봇을 음성 채널에서 내보냅니다")
    async def out_command(self, interaction: discord.Interaction):
        logger.info("/나가 명령어 | 사용자: %s | 현재 상태: %s",
                    interaction.user.display_name, self.state.name)
        self._is_leaving = True
        try:
            if interaction.guild.voice_client is None or self.state == PlayerState.IDLE:
                await interaction.response.send_message("현재 음성 채널에 연결되어 있지 않습니다.")
                return
            self._cancel_idle()
            if not await self._do_leave():
                await interaction.response.send_message("음성 채널 탈퇴 실패")
                return
            self._go_idle()
            await interaction.response.send_message("음성 채널에서 나갔습니다.")
        finally:
            self._is_leaving = False


# load_extension이 호출할 때 실행되는 진입점
async def setup(bot: commands.Bot):
    await bot.add_cog(Music(bot))
