import asyncio
import discord
import logging
import pytz

from datetime import datetime
from discord.ext import commands
from peewee import DoesNotExist
from seraphsix import constants
from seraphsix.cogs.utils.checks import clan_is_linked, member_has_timezone
from seraphsix.cogs.utils.message_manager import MessageManager
from seraphsix.cogs.utils.paginator import EmbedPages
from seraphsix.database import Clan, ClanMember, Guild, Member
from seraphsix.tasks.the100 import collate_the100_activities

logging.getLogger(__name__)


class GameCog(commands.Cog, name='Game'):
    def __init__(self, bot):
        self.bot = bot

    @commands.group()
    async def game(self, ctx):
        """Game Specific Commands"""
        if ctx.invoked_subcommand is None:
            raise commands.CommandNotFound()

    @game.command()
    @clan_is_linked()
    @commands.guild_only()
    async def list(self, ctx):
        """List games on the100 in the linked group(s)"""
        await ctx.trigger_typing()
        manager = MessageManager(ctx)

        clan_dbs = await self.bot.database.get_clans_by_guild(ctx.guild.id)
        game_tasks = [
            self.bot.the100.get_group_gaming_sessions(clan_db.the100_group_id)
            for clan_db in clan_dbs
            if clan_db.the100_group_id
        ]

        results = await asyncio.gather(*game_tasks)

        games = []
        for result in results:
            if isinstance(result, dict) and result.get('error'):
                logging.error(result)
                continue
            games.extend(result)

        if not games:
            await manager.send_message("No the100 game sessions found")
            return await manager.clean_messages()

        embeds = []
        for game in games:
            try:
                spots_reserved = game['party_size'] - 1
            except TypeError:
                continue

            start_time = datetime.fromisoformat(
                game['start_time']).astimezone(tz=pytz.utc)

            embed = discord.Embed(
                color=constants.BLUE,
            )
            embed.set_thumbnail(
                url=(constants.THE100_LOGO_URL)
            )
            embed.add_field(
                name="Activity",
                value=f"[{game['category']}](https://www.the100.io/gaming_sessions/{game['id']})"
            )
            embed.add_field(
                name="Start Time",
                value=start_time.strftime(constants.THE100_DATE_DISPLAY)
            )
            embed.add_field(
                name='Description',
                value=game['name'],
                inline=False
            )

            primary = []
            reserve = []
            for session in game['confirmed_sessions']:
                gamertag = session['user']['gamertag']
                try:
                    query = Member.select(Member, ClanMember, Clan, Guild).join(ClanMember).join(
                        Clan).join(Guild).where(Member.the100_id == session['user_id'])
                    member_db = await self.bot.database.get(query)
                except DoesNotExist:
                    pass
                else:
                    if member_db.clanmember.clan.guild.guild_id == ctx.guild.id:
                        gamertag = f"{gamertag} (m)"

                if session['reserve_spot']:
                    reserve.append(gamertag)
                else:
                    primary.append(gamertag)

            embed.add_field(
                name=(
                    f"Players Joined: {game['primary_users_count']}/{game['team_size']} "
                    f"(Spots Reserved: {spots_reserved})"
                ),
                value=', '.join(primary),
                inline=False
            )
            embed.add_field(
                name='Reserves',
                value=', '.join(reserve) or 'None',
                inline=False
            )
            embed.set_footer(
                text=(
                    f"Creator: {game['creator_gamertag']} | "
                    f"Group: {game['group_name']} | "
                    f"(m) denotes clan member"
                )
            )

            embeds.append(embed)

        paginator = EmbedPages(ctx, embeds)
        await paginator.paginate()

    @game.command()
    @clan_is_linked()
    @member_has_timezone()
    @commands.guild_only()
    async def create(self, ctx):
        """Create a game on the100"""
        await ctx.trigger_typing()
        manager = MessageManager(ctx)

        base_embed = discord.Embed(
            color=constants.BLUE,
        )
        base_embed.set_thumbnail(
            url=(constants.THE100_LOGO_URL)
        )
        for field in ['Status', 'Activity', "Start Time", 'Description', 'Platform', "Group Only"]:
            kwargs = dict(
                name=field,
                value="Not Set",
                inline=True
            )
            if field in ['Description', 'Status']:
                kwargs['inline'] = False
            if field == 'Status':
                kwargs['value'] = "**Game Creation In Progress...**"
            base_embed.add_field(**kwargs)

        game_embed = await manager.send_embed(base_embed)

        # TODO: Figure out how to sanitize the Destiny 1 game activity list
        game_name = "Destiny 2"
        game = await self.bot.the100.get_game_by_name(game_name)
        game_activities, game_activities_by_id = collate_the100_activities(
            game['game_activities'], game_name)

        activity_id = None
        while not activity_id:
            reacts = {}
            for i, activity in enumerate(game_activities.keys()):
                reacts[constants.EMOJI_LETTERS[i]] = activity

            embed = discord.Embed(
                color=constants.BLUE,
                description='\n'.join([f'{react} - {activity}' for react, activity in reacts.items()]),
            )

            react = await manager.send_message_react(
                message_text="Which activity?",
                embed=embed,
                reactions=reacts.keys(),
                clean=False,
                with_cancel=True
            )

            if not react:
                await manager.send_message("Canceling post")
                return await manager.clean_messages()

            activity_react = game_activities[reacts[react]]
            if isinstance(activity_react, int):
                activity_id = activity_react
            else:
                game_activities = activity_react

        base_embed.set_field_at(1, name='Activity', value=game_activities_by_id[activity_id])
        await game_embed.edit(embed=base_embed)

        time = await manager.send_and_get_response(
            "Enter time in the format `6/13 10:00pm` (enter `cancel` to cancel post)")
        if time.lower() == 'cancel':
            await manager.send_message("Canceling post")
            return await manager.clean_messages()

        member_db = await self.bot.database.get(Member, discord_id=ctx.author.id)

        time_format = datetime.strptime(time, constants.THE100_DATE_CREATE).replace(
            year=datetime.now().year).astimezone(tz=pytz.timezone(member_db.timezone))

        base_embed.set_field_at(2, name="Start Time",
                                value=time_format.strftime(constants.THE100_DATE_DISPLAY))
        await game_embed.edit(embed=base_embed)

        description = await manager.send_and_get_response(
            "Enter a description (enter `cancel` to cancel post)")
        if description.lower() == 'cancel':
            await manager.send_message("Canceling post")
            return await manager.clean_messages()

        base_embed.set_field_at(3, name='Description', value=description, inline=False)
        await game_embed.edit(embed=base_embed)

        platforms = {
            constants.EMOJI_XBOX: 'xbox',
            constants.EMOJI_PSN: 'psn',
            constants.EMOJI_PC: 'pc'
        }

        platform = await manager.send_message_react(
            message_text="Which platform?",
            reactions=platforms.keys(),
            clean=False,
            with_cancel=True
        )

        if not platform:
            await manager.send_message("Canceling post")
            return await manager.clean_messages()

        base_embed.set_field_at(4, name='Platform', value=self.bot.get_emoji(platform.id))
        await game_embed.edit(embed=base_embed)

        group_only = {
            constants.EMOJI_CHECKMARK: 'group',
            constants.EMOJI_CROSSMARK: ''
        }

        group = await manager.send_message_react(
            message_text="Group only?",
            reactions=group_only.keys(),
            clean=False,
            with_cancel=True
        )

        if not group:
            await manager.send_message("Canceling post")
            return await manager.clean_messages()

        base_embed.set_field_at(5, name="Group Only", value=group)
        await game_embed.edit(embed=base_embed)

        base_embed.set_field_at(
            0, name='Status', value="**Ready to post, please confirm details...**", inline=False)
        await game_embed.edit(embed=base_embed)

        confirm = {
            constants.EMOJI_CHECKMARK: True,
            constants.EMOJI_CROSSMARK: False
        }

        confirm_res = await manager.send_message_react(
            message_text="Create game?",
            reactions=confirm.keys(),
            clean=False
        )
        if confirm_res == constants.EMOJI_CROSSMARK:
            await manager.send_message("Canceling post")
            return await manager.clean_messages()

        message = ' '.join([group_only[group], platforms[platform.id],
                            game_activities_by_id[activity_id], time_format.strftime('%Y-%m-%dT%H:%M:%S%z'),
                            f"\"{description}\""])

        data = {
            'guild_id': ctx.guild.id,
            'username': ctx.author.name,
            'discriminator': ctx.author.discriminator,
            'message': message
        }

        response = await self.bot.the100.create_gaming_session_discord(data)
        response_msg = response['notice']

        if "Gaming Session Created!" not in response_msg:
            await manager.send_message(response_msg)
        else:
            msg_parts = response_msg.strip().split(' ')
            msg = ' '.join(msg_parts[0:3])
            link = msg_parts[-1]
            base_embed.set_field_at(0, name='Status', value=f"**[{msg}]({link})**", inline=False)
            await game_embed.edit(embed=base_embed)
            manager.remove_message_from_clean(game_embed)
        return await manager.clean_messages()


def setup(bot):
    bot.add_cog(GameCog(bot))