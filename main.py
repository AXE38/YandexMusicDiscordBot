import asyncio
import itertools
import os
import sys
import traceback
from functools import partial

import discord
import mutagen
import wget
from async_timeout import timeout
from discord.ext import commands
from yandex_music import Client
from youtube_dl import YoutubeDL
import asyncio
import yt_dlp
from mutagen.easyid3 import EasyID3
from configparser import ConfigParser
import logging


class Utils:
    # 1 - трек яндекс музыки
    # 2 - видео с ютуба
    # 3 - плейлист яндекс музыки
    # 4 - альбом яндекс музыки
    @staticmethod
    def parse_url(url: str):
        if 'yandex' in url:
            if 'users' in url and 'playlists' in url:
                return 3
            elif 'album' in url and 'track' not in url:
                return 4
            else:
                return 1
        if 'youtu' in url:
            return 2
        return -1

    @staticmethod
    def write_metadata(full_file_path: str, track_name: str, artist: str):
        try:
            audiofile = EasyID3(full_file_path)
        except mutagen.id3.ID3NoHeaderError:
            audiofile = mutagen.File(full_file_path, easy=True)
            audiofile.add_tags()
        audiofile['artist'] = artist
        audiofile['title'] = track_name

        audiofile.save();

    @staticmethod
    def get_metadata(full_file_path: str):
        audiofile = EasyID3(full_file_path)
        return {'artist': audiofile['artist'], 'title': audiofile['title']}


sys.stderr = open('err_log.txt', 'w')
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    stream=sys.stderr
)

parser = ConfigParser()
parser.read("config.ini")

YMClient = Client(parser.get('tokens', 'yandex_token')).init()

ffmpegopts: dict[str, str] = {
    'options': '-vn'
}


class CachePlayer(discord.PCMVolumeTransformer):
    def __init__(self, source, *, data, requester):
        super().__init__(source)
        self.requester = requester

        self.title = data.get('title')
        self.web_url = data.get('webpage_url')

        # YTDL info dicts (data) have other useful information you might want
        # https://github.com/rg3/youtube-dl/blob/master/README.md

    def __getitem__(self, item: str):
        """Allows us to access attributes similar to a dict.
        This is only useful when you are NOT downloading.
        """
        return self.__getattribute__(item)

    @classmethod
    async def create_source(cls, ctx, search: str, *, loop):
        title = search.split(sep='\\\\').pop()

        return {'webpage_url': search, 'requester': ctx.author, 'title': title}

    @classmethod
    async def regather_stream(cls, data):
        requester = data['requester']

        return cls(
            discord.FFmpegPCMAudio(source=data['webpage_url'], options=ffmpegopts,
                                   executable=parser.get('main', 'ffmpeg_path')),
            data=data,
            requester=requester)


class YMPlayer(discord.PCMVolumeTransformer):

    def __init__(self, source, *, data, requester):
        super().__init__(source)
        self.requester = requester

        self.title = data.get('title')
        self.web_url = data.get('webpage_url')

        # YTDL info dicts (data) have other useful information you might want
        # https://github.com/rg3/youtube-dl/blob/master/README.md

    def __getitem__(self, item: str):
        """Allows us to access attributes similar to a dict.
        This is only useful when you are NOT downloading.
        """
        return self.__getattribute__(item)

    @staticmethod
    def parse_track(url: str) -> str:
        url_arr = str.split(url, '/')
        url_arr.reverse()

        return str.split(url_arr[0], '?')[0] + ':' + url_arr[2]

    @staticmethod
    def parse_playlist(url: str) -> str:
        url_arr: list[str] = str.split(url, '/')
        url_arr.reverse()

        return YMPlayer.get_user_uid(url_arr[2]) + ':' + str.split(url_arr[0], '?')[0]

    @staticmethod
    def parse_album(url: str) -> str:
        url_arr: list[str] = str.split(url, '/')
        url_arr.reverse()

        return str.split(url_arr[0], '?')[0]

    @staticmethod
    def get_user_uid(user_name: str) -> str:
        return str(YMClient.request.get(YMClient.base_url + '/users/' + user_name)['uid'])

    @classmethod
    async def create_source(cls, ctx, search: str, *, loop, track_id: str = None):
        loop = loop or asyncio.get_event_loop()

        if track_id is None:
            track_id: str = cls.parse_track(search)

        return {'webpage_url': track_id, 'requester': ctx.author, 'type': "ym"}

    @classmethod
    async def process_playlist(cls, ctx, search: str, *, loop):
        loop = loop or asyncio.get_event_loop()

        playlist_id: str = cls.parse_playlist(search)
        to_run = partial(YMClient.playlists_list, playlist_ids=playlist_id)
        data = await loop.run_in_executor(None, to_run)
        # data = YMClient.playlists_list(playlist_ids=playlist_id)
        tracks = data[0].fetch_tracks()
        source = list()
        for track in tracks:
            source.append(await cls.create_source(ctx, None, loop=loop, track_id=track.track.track_id))
        await ctx.send(f'```ini\n[Playlist {data[0].title} added to the Queue.]\n```')
        return source

    @classmethod
    async def process_album(cls, ctx, search: str, *, loop):
        loop = loop or asyncio.get_event_loop()

        album_id = cls.parse_album(search)

        to_run = partial(YMClient.albums_with_tracks, album_id=album_id)
        data = await loop.run_in_executor(None, to_run)

        source = list()
        for disc in data.volumes:
            for track in disc:
                source.append(await cls.create_source(ctx, None, loop=loop, track_id=track.track_id))

        await ctx.send(f'```ini\n[Album {data.title} added to the Queue.]\n```')
        return source

    @classmethod
    async def regather_stream(cls, data, ctx, loop):
        requester = data['requester']

        track_id = data['webpage_url']

        os.makedirs(os.path.abspath(os.curdir) + '\\cache\\' + str(ctx.guild.id), exist_ok=True)
        full_file_name = os.path.abspath(os.curdir) \
                         + '\\cache\\' \
                         + str(ctx.guild.id) \
                         + '\\' \
                         + track_id.replace(':', '_') \
                         + '.mp3'
        full_file_name = full_file_name.replace('\\', '/')
        track_name = ""
        artist = ""
        if not os.path.isfile(full_file_name):
            to_run = partial(YMClient.tracks, track_ids=track_id)
            data = await loop.run_in_executor(None, to_run)
            url: str = data[0].getDownloadInfo()[0].getDirectLink()
            wget.download(url, out=full_file_name)
            track_name = data[0]['title'] + (' ' + data[0]['version'] if data[0]['version'] is not None else '')
            for i in data[0].artists:
                artist += i.name + ','
            artist = artist[:-1]
            Utils.write_metadata(full_file_name, track_name, artist)
        else:
            res = Utils.get_metadata(full_file_name)
            track_name = res['title'][0]
            artist = res['artist'][0]
        title = (artist + ' - ' if len(artist) > 0 else '') + track_name

        await ctx.send(f'```ini\n[Track {title} added to the Queue.]\n```')

        return cls(
            discord.FFmpegPCMAudio(source=full_file_name, options=ffmpegopts,
                                   executable=parser.get('main', 'ffmpeg_path')),
            data={'webpage_url': full_file_name, 'requester': ctx.author, 'title': title},
            requester=requester)


intents = discord.Intents().all()

bot = commands.Bot(command_prefix='/', intents=intents)

ytdlopts = {
    'format': 'bestaudio/best',
    'outtmpl': 'downloads/%(extractor)s-%(id)s-%(title)s.%(ext)s',
    'restrictfilenames': True,
    'noplaylist': True,
    'nocheckcertificate': True,
    'ignoreerrors': False,
    'logtostderr': False,
    'quiet': True,
    'no_warnings': True,
    'default_search': 'auto',
    'source_address': '0.0.0.0'  # ipv6 addresses cause issues sometimes
}

ytdl = yt_dlp.YoutubeDL(ytdlopts)


class VoiceConnectionError(commands.CommandError):
    """Custom Exception class for connection errors."""


class InvalidVoiceChannel(VoiceConnectionError):
    """Exception for cases of invalid Voice Channels."""


class YTDLSource(discord.PCMVolumeTransformer):

    def __init__(self, source, *, data, requester):
        super().__init__(source)
        self.requester = requester

        self.title = data.get('title')
        self.web_url = data.get('webpage_url')

    def __getitem__(self, item: str):
        return self.__getattribute__(item)

    @classmethod
    async def create_source(cls, ctx, search: str, *, loop):
        loop = loop or asyncio.get_event_loop()

        to_run = partial(ytdl.extract_info, url=search, download=False)
        data = await loop.run_in_executor(None, to_run)

        if 'entries' in data:
            # take first item from a playlist
            data = data['entries'][0]

        await ctx.send(f'```ini\n[Added {data["title"]} to the Queue.]\n```')

        return {'webpage_url': data['webpage_url'], 'requester': ctx.author, 'title': data['title'], 'type': "youtube"}

    @classmethod
    async def regather_stream(cls, data, *, loop):
        """Used for preparing a stream, instead of downloading.
        Since Youtube Streaming links expire."""
        loop = loop or asyncio.get_event_loop()
        requester = data['requester']

        to_run = partial(ytdl.extract_info, url=data['webpage_url'], download=False)
        data = await loop.run_in_executor(None, to_run)
        return cls(discord.FFmpegPCMAudio(data['url'], options=ffmpegopts,
                                          executable=parser.get('main', 'ffmpeg_path')), data=data,
                   requester=requester)


class MusicPlayer(commands.Cog):
    """A class which is assigned to each guild using the bot for Music.
    This class implements a queue and loop, which allows for different guilds to listen to different playlists
    simultaneously.
    When the bot disconnects from the Voice it's instance will be destroyed.
    """

    __slots__ = ('bot', '_guild', '_channel', '_cog', 'queue', 'next', 'current', 'np', 'volume')

    def __init__(self, ctx):
        self.bot = ctx.bot
        self._guild = ctx.guild
        self._channel = ctx.channel
        self._cog = ctx.cog

        self.queue = asyncio.Queue()
        self.next = asyncio.Event()

        self.np = None  # Now playing message
        self.volume = .5
        self.current = None

        ctx.bot.loop.create_task(self.player_loop(ctx))

    async def player_loop(self, ctx):
        """Our main player loop."""
        await self.bot.wait_until_ready()

        while not self.bot.is_closed():
            self.next.clear()

            try:
                # Wait for the next song. If we timeout cancel the player and disconnect...
                async with timeout(3600):  # 1 час
                    source = await self.queue.get()
            except asyncio.TimeoutError:
                return self.destroy(self._guild)

            if not isinstance(source, YTDLSource):
                # Source was probably a stream (not downloaded)
                # So we should regather to prevent stream expiration
                try:
                    if source['type'] == 'ym':
                        source = await YMPlayer.regather_stream(source, ctx, self.bot.loop)
                    elif source['type'] == 'youtube':
                        source = await YTDLSource.regather_stream(source, loop=self.bot.loop)
                except Exception as e:
                    await self._channel.send(f'There was an error processing your song.\n'
                                             f'```css\n[{e}]\n```')
                    continue

            #            source.volume = 0.5# self.volume
            self.current = source

            self._guild.voice_client.play(source, after=lambda _: self.bot.loop.call_soon_threadsafe(self.next.set))
            self.np = await self._channel.send(f'**Now Playing:** `{source.title}` requested by '
                                               f'`{source.requester}`')
            await self.next.wait()

            # Make sure the FFmpeg process is cleaned up.
            source.cleanup()
            self.current = None

            try:
                # We are no longer playing this song...
                await self.np.delete()
            except discord.HTTPException:
                pass

    def destroy(self, guild):
        """Disconnect and cleanup the player."""
        return self.bot.loop.create_task(self._cog.cleanup(guild))


class Music(commands.Cog):
    """Music related commands."""

    __slots__ = ('bot', 'players')

    def __init__(self, bot):
        self.bot = bot
        self.players = {}

    async def cleanup(self, guild):
        try:
            await guild.voice_client.disconnect()
        except AttributeError:
            pass

        try:
            del self.players[guild.id]
        except KeyError:
            pass

    async def __local_check(self, ctx):
        """A local check which applies to all commands in this cog."""
        if not ctx.guild:
            raise commands.NoPrivateMessage
        return True

    async def __error(self, ctx, error):
        """A local error handler for all errors arising from commands in this cog."""
        if isinstance(error, commands.NoPrivateMessage):
            try:
                return await ctx.send('This command can not be used in Private Messages.')
            except discord.HTTPException:
                pass
        elif isinstance(error, InvalidVoiceChannel):
            await ctx.send('Error connecting to Voice Channel. '
                           'Please make sure you are in a valid channel or provide me with one')

        print('Ignoring exception in command {}:'.format(ctx.command), file=sys.stderr)
        traceback.print_exception(type(error), error, error.__traceback__, file=sys.stderr)

    def get_player(self, ctx):
        """Retrieve the guild player, or generate one."""
        try:
            player = self.players[ctx.guild.id]
        except KeyError:
            player = MusicPlayer(ctx)
            self.players[ctx.guild.id] = player

        return player

    @commands.command(name='connect', aliases=['join'])
    async def connect_(self, ctx):
        try:
            channel = ctx.author.voice.channel
        except AttributeError:
            raise InvalidVoiceChannel('No channel to join.')

        vc = ctx.voice_client

        if vc:
            if vc.channel.id == channel.id:
                return
            try:
                await vc.move_to(channel)
            except asyncio.TimeoutError:
                raise VoiceConnectionError(f'Moving to channel: <{channel}> timed out.')
        else:
            try:
                await channel.connect()
            except asyncio.TimeoutError:
                raise VoiceConnectionError(f'Connecting to channel: <{channel}> timed out.')
        embed = discord.Embed(title="Joined A Call")
        embed.add_field(name="Connected To :", value=channel, inline=True)

        await ctx.send(embed=embed)

    @commands.command(name='play', aliases=['p'])
    async def play_(self, ctx, *, search: str):
        await ctx.typing()

        vc = ctx.voice_client

        if not vc:
            await ctx.invoke(self.connect_)

        player = self.get_player(ctx)

        source = list()
        url_type = Utils.parse_url(search)
        if url_type == 2:
            source.append(await YTDLSource.create_source(ctx, search, loop=self.bot.loop))
        elif url_type == 1:
            source.append(await YMPlayer.create_source(ctx, search, loop=self.bot.loop))
        elif url_type == 3:
            source = await YMPlayer.process_playlist(ctx, search, loop=self.bot.loop)
        elif url_type == 4:
            source = await YMPlayer.process_album(ctx, search, loop=self.bot.loop)
        else:
            await ctx.send('Неправильная ссылка')

        if len(source) > 0:
            for i in source:
                await player.queue.put(i)

    @commands.command(name='cache', aliases=['c', 'random'])
    async def cache_(self, ctx):
        await ctx.trigger_typing()

        vc = ctx.voice_client

        if not vc:
            await ctx.invoke(self.connect_)

        player = self.get_player(ctx)

        file_path = os.path.abspath(os.curdir) + '\\cache\\' + str(ctx.guild.id)

        for file in os.listdir(os.fsencode(file_path)):
            file_name = os.path.basename(file)
            print(file_name)
            source = await CachePlayer.create_source(ctx, file_name, loop=self.bot.loop)

            if source is not None:
                await player.queue.put(source)

    @commands.command(name='pause')
    async def pause_(self, ctx):
        """Pause the currently playing song."""
        vc = ctx.voice_client

        if not vc or not vc.is_playing():
            return await ctx.send('I am not currently playing anything!')
        elif vc.is_paused():
            return

        vc.pause()
        await ctx.send(f'**`{ctx.author}`**: Paused the song!')

    @commands.command(name='resume', aliases=['unpause'])
    async def resume_(self, ctx):
        """Resume the currently paused song."""
        vc = ctx.voice_client

        if not vc or not vc.is_connected():
            return await ctx.send('I am not currently playing anything!', )
        elif not vc.is_paused():
            return

        vc.resume()
        await ctx.send(f'**`{ctx.author}`**: Resumed the song!')

    @commands.command(name='skip')
    async def skip_(self, ctx, *, search: str = None):
        """Skip the song."""
        vc = ctx.voice_client

        if not vc or not vc.is_connected():
            return await ctx.send('I am not currently playing anything!')

        if vc.is_paused():
            pass
        elif not vc.is_playing():
            return

        try:
            cnt = int(search)
        except:
            cnt = 1
        if cnt > 1:
            player = self.get_player(ctx)
            for i in range(cnt - 1):
                await player.queue.get()

        vc.stop()
        await ctx.send(f'**`{ctx.author}`**: Skipped the song!')

    @commands.command(name='queue', aliases=['q', 'playlist'])
    async def queue_info(self, ctx):
        """Retrieve a basic queue of upcoming songs."""
        vc = ctx.voice_client

        if not vc or not vc.is_connected():
            return await ctx.send('I am not currently connected to voice!')

        player = self.get_player(ctx)
        if player.queue.empty():
            return await ctx.send('There are currently no more queued songs.')

        # Grab up to 5 entries from the queue...
        upcoming = list(itertools.islice(player.queue._queue, 0, 5))

        fmt = '\n'.join(f'**`{_["title"]}`**' for _ in upcoming)
        embed = discord.Embed(title=f'Upcoming - Next {len(upcoming)}', description=fmt)

        await ctx.send(embed=embed)

    @commands.command(name='now_playing', aliases=['np', 'current', 'currentsong', 'playing'])
    async def now_playing_(self, ctx):
        """Display information about the currently playing song."""
        vc = ctx.voice_client

        if not vc or not vc.is_connected():
            return await ctx.send('I am not currently connected to voice!', )

        player = self.get_player(ctx)
        if not player.current:
            return await ctx.send('I am not currently playing anything!')

        try:
            # Remove our previous now_playing message.
            await player.np.delete()
        except discord.HTTPException:
            pass

        player.np = await ctx.send(f'**Now Playing:** `{vc.source.title}` '
                                   f'requested by `{vc.source.requester}`')

    @commands.command(name='volume', aliases=['vol'])
    async def change_volume(self, ctx, *, vol: float):
        """Change the player volume.
        Parameters
        ------------
        volume: float or int [Required]
            The volume to set the player to in percentage. This must be between 1 and 100.
            :param vol:
        """
        vc = ctx.voice_client

        if not vc or not vc.is_connected():
            return await ctx.send('I am not currently connected to voice!', )

        if not 0 < vol < 101:
            return await ctx.send('Please enter a value between 1 and 100.')

        player = self.get_player(ctx)

        if vc.source:
            vc.source.volume = vol / 100

        player.volume = vol / 100
        embed = discord.Embed(title="Volume Message",
                              description=f'The Volume Was Changed By **{ctx.author.name}**')
        embed.add_field(name="Current Volume", value=vol, inline=True)
        await ctx.send(embed=embed)
        # await ctx.send(f'**`{ctx.author}`**: Set the volume to **{vol}%**')

    @commands.command(name='stop', aliases=['leave'])
    async def stop_(self, ctx):
        """Stop the currently playing song and destroy the player.
        !Warning!
            This will destroy the player assigned to your guild, also deleting any queued songs and settings.
        """
        vc = ctx.voice_client

        if not vc or not vc.is_connected():
            return await ctx.send('I am not currently playing anything!')

        await self.cleanup(ctx.guild)


TOKEN = parser.get('tokens', 'discord_token')


async def main():
    async with bot:
        await bot.add_cog(Music(bot))
        await bot.start(TOKEN)

asyncio.run(main())
