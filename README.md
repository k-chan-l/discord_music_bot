# Discord Music Bot

Discord 서버에서 개인적으로 사용하기 위해 제작한 음악 재생 봇입니다.  
YouTube URL 또는 검색어로 음악을 재생하며, Docker 기반으로 배포합니다.

---

## 기술 스택

| 분류 | 기술 |
|------|------|
| 언어 | Python 3.14 |
| Discord | discord.py 2.x (Slash Command, Voice Client, UI) |
| 미디어 | yt-dlp, FFmpeg |
| 배포 | Docker |

---

## 주요 기능

- YouTube URL 또는 검색어로 음악 재생
- 대기열(Queue) 기반 순차 재생 및 자동 다음 곡 재생
- Embed + 버튼 UI로 현재 재생 곡 제어
- 일정 시간 재생 없으면 음성 채널 자동 퇴장
- Docker 컨테이너 기반 배포

---

## 명령어

| 명령어 | 설명 |
|--------|------|
| `/재생 <URL 또는 검색어>` | 음악 재생 또는 대기열에 추가 |
| `/나가` | 음성 채널에서 퇴장 |

버튼 UI (재생 중 Embed에 표시):

| 버튼 | 기능 |
|------|------|
| ⏮️ | 이전 곡 |
| ⏯️ | 일시정지 / 재개 |
| ⏭️ | 다음 곡 |
| 🔁 | 반복 재생 토글 |

---

## 프로젝트 구조

```
discord_music_bot/
├── main.py              # 봇 초기화, Cog 로딩, Slash Command 동기화
├── yt_music_play.py     # 음악 재생 로직 전체 (Cog, Queue, UI, yt-dlp 연동)
├── requirements.txt
├── dockerfile
├── build.sh             # 이미지 빌드 및 컨테이너 실행
├── run.sh               # 컨테이너 재시작
├── .env                 # DISCORD_TOKEN, GUILD_ID (git 제외)
├── .dockerignore
└── .gitignore
```

`yt_music_play.py` 내부 구성:

```
fetch_song()    — URL 또는 검색어로 Song 메타데이터 조회 (비동기)
fetch_audio()   — Song의 스트리밍 URL 추출 후 FFmpegPCMAudio 반환 (비동기)
Song            — 곡 메타데이터 데이터 클래스
SongQueue       — deque 기반 대기열 관리
MusicView       — 버튼 UI (discord.ui.View)
Music (Cog)     — 슬래시 커맨드, 상태 머신, 재생 루프
```

---

## 핵심 설계

### 1. 상태 머신 기반 재생 관리

음악 봇의 동작을 `PlayerState` Enum으로 명시적으로 관리했습니다.  
상태에 따라 다음 동작이 달라지기 때문에, 각 전이를 명확히 분리해 예외 상황을 줄였습니다.

```
IDLE ──/재생──► PLAYING ──일시정지──► PAUSED
  ▲                │                    │
  └──타임아웃──── WAITING ◄──큐 소진────┘
```

```python
class PlayerState(Enum):
    IDLE    = auto()   # 초기 상태, 음성 채널 미연결
    WAITING = auto()   # 연결됐지만 재생할 곡 없음 (타임아웃 대기 중)
    PLAYING = auto()   # 재생 중
    PAUSED  = auto()   # 일시정지
```

상태 전이는 각각 독립된 메서드(`_go_idle`, `_go_waiting`, `_go_playing`)로 분리해  
어떤 케이스에서도 상태가 명확하게 유지되도록 했습니다.

---

### 2. 이벤트 루프 블로킹 방지

yt-dlp는 동기(sync) 라이브러리라, 비동기 컨텍스트에서 직접 호출하면 Discord 이벤트 루프 전체가 멈춥니다.  
`run_in_executor`로 yt-dlp 호출을 별도 스레드에서 실행해 봇이 멈추지 않도록 했습니다.

```python
async def fetch_song(query: str, user: discord.User) -> Song | None:
    def extract():
        with yt_dlp.YoutubeDL(YTDL_OPTS) as ydl:
            return ydl.extract_info(search, download=False)

    loop = asyncio.get_running_loop()
    info = await loop.run_in_executor(None, extract)  # 별도 스레드에서 실행
    ...
```

---

### 3. after 콜백과 이벤트 루프 연동

`voice_client.play()`의 `after` 콜백은 재생이 끝났을 때 **별도 스레드**에서 실행됩니다.  
코루틴을 직접 호출할 수 없어 `run_coroutine_threadsafe`로 이벤트 루프에 등록합니다.

```python
self.voice_client.play(
    player,
    after=lambda e: asyncio.run_coroutine_threadsafe(
        self._after_play(e), self.bot.loop
    ),
)
```

---

### 4. Cog + load_extension 구조

discord.py 공식 방식인 `load_extension`으로 Cog을 분리해 로딩합니다.  
`main.py`는 봇 초기화만 담당하고, 음악 로직 전체는 `yt_music_play.py`에 캡슐화됩니다.

```python
# main.py
async def setup_hook(self):
    await self.load_extension("yt_music_play")
    self.tree.copy_global_to(guild=MY_GUILD)
    await self.tree.sync(guild=MY_GUILD)

# yt_music_play.py
async def setup(bot: commands.Bot):  # load_extension이 호출하는 진입점
    await bot.add_cog(Music(bot))
```

---

## 실행 방법

### 1. 저장소 클론

```bash
git clone https://github.com/k-chan-l/discord_music_bot.git
cd discord_music_bot
```

### 2. `.env` 파일 생성

```env
DISCORD_TOKEN=your_token_here
GUILD_ID=your_guild_id_here
```

### 3. 빌드 및 실행

```bash
./build.sh
```

### 4. 재시작 (코드 수정 없이 컨테이너만 재시작)

```bash
./run.sh
```

---

## Docker 구성

```dockerfile
FROM python:3.14-slim

WORKDIR /app

RUN apt-get update && apt-get install -y ffmpeg curl unzip && rm -rf /var/lib/apt/lists/*

ENV DENO_INSTALL=/root/.deno
ENV PATH="${DENO_INSTALL}/bin:${PATH}"

RUN curl -fsSL https://deno.land/install.sh | sh

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "main.py"]
```

FFmpeg와 Deno를 별도 설치하고, 나머지 의존성은 `requirements.txt`로 관리합니다.  
`--env-file .env`로 토큰 등 민감한 정보를 컨테이너에 주입해 코드에서 분리했습니다.

---

## 개발하면서 고민하고 해결한 것들

### 비동기 환경에서 동기 라이브러리 사용

yt-dlp가 동기 라이브러리라는 것을 처음에 인지하지 못해,  
음악 검색 중 봇 전체가 수 초간 응답하지 않는 문제가 있었습니다.

Discord의 이벤트 루프가 싱글 스레드로 동작하기 때문에, 동기 I/O가 루프를 점유하면  
그 시간 동안 버튼 클릭이나 다른 명령어가 전부 무시됩니다.

`run_in_executor`로 yt-dlp 호출을 스레드풀로 분리해 이벤트 루프가 끊기지 않도록 해결했습니다.

---

### after 콜백이 다른 스레드에서 실행된다는 점

`voice_client.play(after=...)`의 콜백이 이벤트 루프 바깥의 별도 스레드에서 실행된다는 점을  
파악하는 데 시간이 걸렸습니다.

스레드에서 코루틴을 직접 호출하면 실행되지 않기 때문에,  
`run_coroutine_threadsafe`로 이벤트 루프에 등록하는 방식으로 해결했습니다.

---

### 상태 전이 설계

재생 중 새 곡 요청, 큐 소진, 일시정지, 타임아웃 등 케이스가 많아  
처음에는 상태가 뒤섞이는 문제가 있었습니다.

상태 머신 구조로 설계하고, 각 전이를 독립된 메서드로 분리해  
어떤 케이스에서도 상태가 명확하게 유지되도록 했습니다.

---

### 환경 의존성 문제

FFmpeg, yt-dlp, Python 버전 등 의존성이 많아 환경마다 동작이 달라질 수 있었습니다.  
Docker로 실행 환경 자체를 고정해 서버 환경에서도 동일하게 동작하도록 했습니다.

---

### 보안 파일 분리

Discord Bot Token, Guild ID처럼 외부에 노출되면 안 되는 정보는  
`.env` 파일로 분리하고 `.gitignore`에 포함시켜 코드와 분리했습니다.

Docker 실행 시 `--env-file .env` 옵션으로 컨테이너에 주입하기 때문에,  
토큰 변경이나 서버 변경 시 코드 수정 없이 `.env`만 교체하면 됩니다.
