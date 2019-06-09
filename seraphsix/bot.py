#!/usr/bin/env python3.7
import ast
import asyncio
import discord
import jsonpickle
import logging
import peony
import traceback

from discord.ext import commands
from iron_cache import IronCache
from peewee import DoesNotExist
from peony import PeonyClient
from pydest import Pydest
from the100 import The100

from seraphsix.cogs.utils.message_manager import MessageManager
from seraphsix.constants import SUPPORTED_GAME_MODES
from seraphsix.database import Database

from seraphsix.errors import (
    InvalidCommandError, InvalidGameModeError, InvalidMemberError,
    NotRegisteredError, ConfigurationError)
from seraphsix.tasks.activity import store_member_history, store_last_active

logging.getLogger(__name__)

STARTUP_EXTENSIONS = [
    'seraphsix.cogs.clan', 'seraphsix.cogs.game', 'seraphsix.cogs.member',
    'seraphsix.cogs.register', 'seraphsix.cogs.server'
]


async def _prefix_callable(bot, message):
    """Get current command prefix"""
    base = [f'<@{bot.user.id}> ']
    if isinstance(message.channel, discord.abc.PrivateChannel):
        base.append('?')
    else:
        try:
            guild = await bot.database.get_guild(message.guild.id)
        except DoesNotExist:
            await bot.database.create_guild(message.guild.id)
            base.append('?')
        else:
            base.append(guild.prefix)
    return base


class SeraphSix(commands.Bot):

    TWITTER_DESTINY_REDDIT = 2608131020
    TWITTER_XBOX_SUPPORT = 59804598

    def __init__(self, config):
        super().__init__(
            command_prefix=_prefix_callable, case_insensitive=True,
            help_command=commands.DefaultHelpCommand(
                no_category="Assorted", dm_help=True, verify_checks=False)
        )

        self.config = config
        self.database = Database(config['database_url'], loop=self.loop)
        self.database.initialize()

        self.destiny = Pydest(
            api_key=config['bungie']['api_key'],
            loop=self.loop,
            client_id=config['bungie']['client_id'],
            client_secret=config['bungie']['client_secret']
        )

        self.the100 = The100(config['the100_api_key'], loop=self.loop)

        self.twitter = None
        if (config['twitter'].get('consumer_key') and
                config['twitter'].get('consumer_secret') and
                config['twitter'].get('access_token') and
                config['twitter'].get('access_token_secret')):
            self.twitter = PeonyClient(loop=self.loop, **config['twitter'])

        for extension in STARTUP_EXTENSIONS:
            try:
                self.load_extension(extension)
            except Exception as e:
                exc = traceback.format_exception(type(e), e, e.__traceback__)
                logging.error(f"Failed to load extension {extension}: {exc}")

    async def store_all_games(self, game_mode: str, guild_id: int):
        await self.wait_until_ready()
        while not self.is_closed():
            guild_db = await self.database.get_guild(guild_id)

            try:
                clan_dbs = await self.database.get_clans_by_guild(guild_id)
            except DoesNotExist:
                return

            logging.info(
                f"Finding all {game_mode} games for members of server {guild_id} active in the last hour")

            tasks = []
            member_dbs = []
            for clan_db in clan_dbs:
                if not clan_db.activity_tracking:
                    logging.info(f"Clan activity tracking disabled for Clan {clan_db.name}, skipping")
                    continue

                clan_id = clan_db.id

                if guild_db.aggregate_clans:
                    member_dbs.extend(await self.database.get_clan_members_active(clan_id, hours=1))
                else:
                    member_dbs = await self.database.get_clan_members_active(clan_id, hours=1)

                tasks.extend([
                    store_member_history(
                        member_dbs, self.database, self.destiny, member_db, game_mode)
                    for member_db in member_dbs
                ])

            results = await asyncio.gather(*tasks)

            logging.info(
                f"Found {sum(filter(None, results))} {game_mode} games for members "
                f"of server {guild_id} active in the last hour"
            )
            await asyncio.sleep(3600)

    async def update_last_active(self, guild_id: int):
        await self.wait_until_ready()
        logging.info(
            f"Finding last active dates for all members of {guild_id}")

        members = ast.literal_eval(
            self.caches[guild_id].get('members').value)

        tasks = [
            store_last_active(self.database, self.destiny, jsonpickle.decode(member))
            for member in members
        ]

        await asyncio.gather(*tasks)

    async def process_tweet(self, tweet):
        channels = await self.database.get_twitter_channels(tweet.user.id)
        if not channels:
            logging.info(
                f"Could not find any Discord channels for {tweet.user.screen_name} ({tweet.user.id})")
            return

        twitter_url = f"https://twitter.com/{tweet.user.screen_name}/status/{tweet.id}"
        log_message = f"Sending tweet {tweet.id} by {tweet.user.screen_name} to "

        for channel in channels:
            logging.info(log_message + str(channel.channel_id))
            channel = self.get_channel(channel.channel_id)
            await channel.send(twitter_url)

    async def track_tweets(self):
        await self.wait_until_ready()

        follow_users = [self.TWITTER_XBOX_SUPPORT, self.TWITTER_DESTINY_REDDIT]
        stream = self.twitter.stream.statuses.filter.post(follow=follow_users)
        async for tweet in stream:
            if peony.events.tweet(tweet):
                if tweet.in_reply_to_status_id:
                    continue
                if tweet.user.id not in follow_users:
                    continue
                self.loop.create_task(self.process_tweet(tweet))

    async def build_member_cache(self, guild_id: int):
        await self.wait_until_ready()

        self.caches[guild_id] = IronCache(
            name=str(guild_id), **self.config['iron_cache'])

        members = [
            jsonpickle.encode(member)
            for member in await self.database.get_clan_members_by_guild_id(
                guild_id)
        ]

        self.caches[guild_id].put('members', members)
        logging.info(f"Populated member cache for server {guild_id}")

    async def on_ready(self):
        start_message = (
            f"Logged in as {self.user.name} ({self.user.id}) "
            f"https://discordapp.com/oauth2/authorize?"
            f"client_id={self.user.id}&scope=bot"
        )
        logging.info(start_message)

        self.caches = {}
        guilds = await self.database.get_guilds()

        tasks_cache = []
        tasks_all_games = []
        tasks_last_active_initial = []
        tasks_last_active_repeat = []

        for guild in guilds:
            guild_id = guild.guild_id

            tasks_cache.append(self.build_member_cache(guild_id))
            tasks_last_active_initial.append(self.update_last_active(guild_id))

            for game_mode in SUPPORTED_GAME_MODES.keys():
                if '-' not in game_mode:
                    tasks_all_games.append(self.store_all_games(game_mode, guild_id))

            tasks_last_active_repeat.append(self.repeat_forever(300, self.update_last_active, guild_id))

        await asyncio.gather(*tasks_cache, *tasks_last_active_initial)
        await asyncio.gather(*tasks_all_games, *tasks_last_active_repeat)

        if hasattr(self, 'twitter'):
            logging.info("Starting Twitter stream tracking")
            self.loop.create_task(self.track_tweets())

    async def on_command_error(self, ctx, error):
        manager = MessageManager(ctx)

        text = None
        if isinstance(error, commands.MissingPermissions):
            text = "Sorry, but you do not have permissions to do that!"
        elif isinstance(error, (
            ConfigurationError, InvalidCommandError, InvalidMemberError,
            InvalidGameModeError, NotRegisteredError
        )):
            text = error
        elif isinstance(error, commands.CommandNotFound):
            text = f"Invalid command `{ctx.message.content}`."
        elif isinstance(error, commands.MissingRequiredArgument):
            text = f"Required argument `{error.param}` is missing."
        else:
            error_trace = traceback.format_exception(
                type(error), error, error.__traceback__)
            logging.error(
                f"Ignoring exception in command \"{ctx.command}\": {error_trace}")

        if text:
            await manager.send_message(
                f"{text}\nType `{ctx.prefix}help` for more information.")
            await manager.clean_messages()

    async def on_message(self, message):
        if not message.author.bot:
            ctx = await self.get_context(message)
            await self.invoke(ctx)

    async def repeat_forever(self, period, coroutine, *args, **kwargs):
        while not self.is_closed():
            await coroutine(*args, **kwargs)
            await asyncio.sleep(period)

    async def close(self):
        logging.info("Canceling outstanding tasks.")
        tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        [task.cancel() for task in tasks]
        await asyncio.gather(*tasks)

        self.destiny.close()
        await self.database.close()
        await self.the100.close()
        if self.twitter:
            await self.twitter.close()
        await super().close()
